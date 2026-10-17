# -*- coding: utf-8 -*-
"""重启托底数据回灌工具（v0.6.2）。

配合仓库根 AGENTS.md「服务器重启的数据保全规程」使用，把"服务器重启期间由
本地测试端托底采集的数据"合并回服务器数据库。三个子命令对应规程的三个阶段：

    uv run python deploy/backfill_local_data.py snapshot   # 规程第 2 步前半：
        # SSH 到服务器，用 SQLite 在线备份接口取一致快照拉回本地 data/live.db
        # （不中断服务器采集），并记录快照边界 stamp（各表 max id + 时间下限）。
        # 之后才启动本地测试端 capture。

    uv run python deploy/backfill_local_data.py export     # 规程第 4 步前半：
        # 把本地库中 id 大于快照边界的新数据（弹幕/礼物/互动/人气与主播快照/
        # 身份映射/场次标题/新建场次）打包为 data/backfill-package.json。

    uv run python deploy/backfill_local_data.py apply      # 规程第 4 步后半：
        # 把包上传到服务器并执行合并（默认远程模式；--server-db 指定本地文件
        # 则离线直连，测试与手工恢复用）。--dry-run 只报告不落盘。

设计要点（与 tests/test_backfill.py 的场景一一对应）：

1. 快照边界去重：重启前后存在"两侧同时在线"的重叠窗口（服务器采集开机自启，
   早于我们能停本地端），同一事件会被两侧各记一条。回灌时对每条待导入行，
   仅在服务器侧 **id > 快照边界**（即快照之后服务器新写的行）中查同键记录，
   时间列 ±tol 秒容差匹配（两机时钟存在秒级偏差），其余键列精确相等
   （NULL 用 IS 比较）。命中即视为重叠窗口的重复，跳过不导入。
2. 场次归属不拆场：
   - 快照前就存在的场次（session_id ≤ 边界）：本地端重启后"收养/重开"的
     就是同一行（id 相同），明细直接归位；
   - 窗口内本地新建的场次：优先按"同房间 + 服务器侧快照后新建 + 开播时间
     最接近（≤ --session-match-tol，默认 600s，与采集端 session_merge_gap_sec
     口径一致）"映射到服务器场次；找不到则在服务器插入新场次行，明细随新
     id 归位（该场计数以本地计数直接初始化，不重复累加）。
3. 冗余计数与峰值：映射/旧场次的 danmaku_count、gift_value_yuan 按本次实际
     导入条数增量累加，峰值取导入快照的最大值（与 maintenance 合并逻辑同
     口径，不按明细重算，避免明细保留期导致老场次计数缩水）。
4. user_stats 不回灌：该表由每日维护全量重算覆盖，导入的明细自然并入
   （见 replay/capture/maintenance.py::_recompute_user_stats）。
5. 只用 Python 标准库；服务器端（python3.14）与本地（3.12）均可直接运行
   本文件（apply 远程模式会把本脚本自身 scp 到服务器 /tmp 执行）。

注意：context7 技术调研本次不可用（连接失败）；sqlite3 用法仅涉稳定接口
（backup 在线备份 / WAL 并发 / busy_timeout），与项目 db.py 既有用法一致。
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

TS_FMT = "%Y-%m-%dT%H:%M:%S"

# 路径默认值：脚本位于 <工作区>/replay/deploy/，据此向上推导
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent                       # replay/
WORKSPACE_DIR = PROJECT_DIR.parent                    # 工作区根
DATA_DIR = PROJECT_DIR / "data"
LOCAL_DB = DATA_DIR / "live.db"
STAMP_PATH = DATA_DIR / "backfill-stamp.json"
PACKAGE_PATH = DATA_DIR / "backfill-package.json"

# 服务器连接默认值：公共仓副本不内置任何服务器地址与密钥路径（避免泄漏
# 部署者私有信息）；snapshot 与 apply 远程模式必须显式传 --host / --key。
# --remote-db 保留 /opt 部署约定默认值，可按实际部署路径覆盖。
DEFAULT_HOST = None
DEFAULT_KEY = None
DEFAULT_REMOTE_DB = "/opt/replay/data/live.db"
REMOTE_SNAP_DB = "/tmp/bili_backfill_snap.db"         # 服务器临时快照（用后即删）
REMOTE_TOOL = "/tmp/bili_backfill_tool.py"            # apply 远程模式的自身副本


def _require_host_args(args) -> None:
    """远程模式前置检查：--host / --key 未指定时给出可直接照做的提示。"""
    if not args.host or not args.key:
        raise SystemExit(
            "未指定服务器连接参数。请在命令后追加：\n"
            "  --host root@<你的服务器IP> --key <SSH私钥路径>\n"
            "（apply 离线模式可改用 --server-db <本地库路径>，无需 SSH）")

# 参与回灌的明细表：表名 → 自增主键列（快照边界即各表 max id）。
# anchor_snapshots（主播指标轮询采样）同为只增明细，一并回灌。
DETAIL_TABLES = ("danmaku", "gifts", "interactions",
                 "popularity_snapshots", "anchor_snapshots")

# 每张表的重叠去重规则：keys 为精确相等键（NULL 安全的 IS 比较），
# ts 为时间列（±--tol 秒容差比较，容忍两机时钟偏差）。
# - danmaku/gifts/interactions：同一事件的用户/内容字段两侧必然一致；
# - popularity/anchor 快照：同房间同场次在容差内的采样视为同一采样时刻。
DEDUP_RULES = {
    "danmaku":             {"keys": ["session_id", "uid", "text"], "ts": "ts"},
    "gifts":               {"keys": ["session_id", "uid", "type", "name",
                                     "count", "value_yuan"], "ts": "ts"},
    "interactions":        {"keys": ["session_id", "uid", "type"], "ts": "ts"},
    "popularity_snapshots": {"keys": ["session_id", "room_id"], "ts": "ts"},
    "anchor_snapshots":    {"keys": ["room_id"], "ts": "ts"},
}


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return dt.datetime.now().strftime(TS_FMT)


def ts_shift(ts: str, seconds: int) -> str:
    """ISO 秒级时间串平移 ±seconds（容差比较的上下界；同格式定宽，可字典序比较）。"""
    t = dt.datetime.strptime(ts, TS_FMT) + dt.timedelta(seconds=seconds)
    return t.strftime(TS_FMT)


def ts_diff_secs(a: str, b: str) -> int:
    return int((dt.datetime.strptime(a, TS_FMT) - dt.datetime.strptime(b, TS_FMT))
               .total_seconds())


def table_cols(conn: sqlite3.Connection, table: str) -> list[str]:
    """按建表顺序返回列名（不含自增主键 id）——INSERT 时让服务器自行分配新 id。"""
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})") if r[1] != "id"]


def collect_boundaries(conn: sqlite3.Connection) -> dict:
    """记录快照边界：五张明细表（自增主键 id）+ live_sessions（主键
    session_id）的当前 max 值（无行记 0）。"""
    out = {}
    for t in DETAIL_TABLES:
        out[t] = conn.execute(f"SELECT COALESCE(MAX(id), 0) FROM {t}").fetchone()[0]
    out["live_sessions"] = conn.execute(
        "SELECT COALESCE(MAX(session_id), 0) FROM live_sessions").fetchone()[0]
    return out


def run_ssh(args_list: list[str], key: Path, host: str,
            stdin_text: str | None = None, check: bool = True):
    """执行 ssh 命令（列表参数，不经 shell；stdin_text 可携带远端脚本）。"""
    cmd = ["ssh", "-i", str(key), "-o", "BatchMode=yes",
           "-o", "ConnectTimeout=15", host] + args_list
    return subprocess.run(cmd, input=stdin_text, capture_output=True, text=True,
                          check=check)


# ---------------------------------------------------------------------------
# snapshot：取服务器一致快照到本地并记录边界
# ---------------------------------------------------------------------------

_REMOTE_BACKUP_CODE = """
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[2]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[1])
src.backup(dst)          # 在线备份：对正在写入的 WAL 库取一致性快照
dst.close(); src.close()
print("remote-backup-ok")
"""


def cmd_snapshot(args) -> int:
    _require_host_args(args)
    key, host = Path(args.key), args.host
    gz_remote = REMOTE_SNAP_DB + ".gz"
    gz_local = DATA_DIR / "backfill-snap.db.gz"

    print(f"[snapshot] 目标 {host}:{args.remote_db}")
    # 1) 服务器端在线备份 + 压缩（服务器 gzip 压缩后再传输，缩小到约 1/4）
    run_ssh(["python3", "-", REMOTE_SNAP_DB, args.remote_db],
            key, host, stdin_text=_REMOTE_BACKUP_CODE)
    run_ssh(["gzip", "-kf", REMOTE_SNAP_DB], key, host)
    # 2) 拉回本地（data/ 在 .gitignore 中，产物不入库）
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["scp", "-i", str(key), "-o", "BatchMode=yes",
                        f"{host}:{gz_remote}", str(gz_local)],
                       capture_output=True, text=True, check=True)
    run_ssh(["rm", "-f", REMOTE_SNAP_DB, gz_remote], key, host)  # 清理服务器临时件

    # 3) 本地落位：旧库滚动备份为 live.db.pre-snapshot-<时间>，快照解压为 live.db
    if LOCAL_DB.exists():
        rotated = DATA_DIR / f"live.db.pre-snapshot-{now_iso().replace(':', '')}"
        LOCAL_DB.rename(rotated)
        print(f"[snapshot] 本地旧库已滚动为 {rotated.name}")
    with gzip.open(gz_local, "rb") as fsrc, open(LOCAL_DB, "wb") as fdst:
        fdst.write(fsrc.read())
    gz_local.unlink()

    # 4) 校验 + 记录边界 stamp（ts_floor 供 updated_at/标题类非自增表做时间下限）
    conn = sqlite3.connect(f"file:{LOCAL_DB}?mode=ro", uri=True)
    ok = conn.execute("PRAGMA quick_check").fetchone()[0]
    if ok != "ok":
        print(f"[snapshot] 快照完整性检查失败：{ok}，不写 stamp", file=sys.stderr)
        return 1
    boundaries = collect_boundaries(conn)
    conn.close()
    stamp = {
        "tool": "backfill_local_data", "version": 1, "created_at": now_iso(),
        "host": host, "remote_db": args.remote_db,
        "ts_floor": ts_shift(now_iso(), -60),   # 60s 缓冲容忍两机时钟偏差
        "max": boundaries,
    }
    STAMP_PATH.write_text(json.dumps(stamp, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    print(f"[snapshot] 快照就位：{LOCAL_DB}")
    print(f"[snapshot] 边界 stamp 已写入：{STAMP_PATH}")
    print(f"[snapshot] 边界：{json.dumps(boundaries, ensure_ascii=False)}")
    print("[snapshot] 下一步：启动本地测试端 capture 托底采集；"
          "重启完成后执行 export + apply")
    return 0


# ---------------------------------------------------------------------------
# export：打包本地快照边界之后的新数据
# ---------------------------------------------------------------------------

def build_package(conn: sqlite3.Connection, stamp: dict) -> dict:
    """从本地库读取边界之后的新数据，构造回灌包（纯函数，便于测试）。"""
    maxids = stamp["max"]
    pkg: dict = {
        "tool": "backfill_local_data", "version": 1,
        "created_at": now_iso(), "stamp": stamp,
        "columns": {}, "rows": {}, "new_sessions": [],
        "uname_map": [], "session_titles": [],
    }
    # 明细表：id > 边界的全部行（去 id，导入时让服务器重新分配）
    for t in DETAIL_TABLES:
        cols = table_cols(conn, t)
        pkg["columns"][t] = cols
        rows = conn.execute(
            f"SELECT {','.join(cols)} FROM {t} WHERE id > ? ORDER BY id",
            (maxids[t],)).fetchall()
        pkg["rows"][t] = [dict(zip(cols, r)) for r in rows]
    # 窗口内本地新建的场次（含计数与峰值；apply 端决定映射或插入）
    sess_cols = ("room_id", "start_time", "end_time", "peak_popularity",
                 "peak_viewers", "danmaku_count", "gift_value_yuan")
    for r in conn.execute(
            f"SELECT session_id,{','.join(sess_cols)} FROM live_sessions "
            f"WHERE session_id > ? ORDER BY session_id", (maxids["live_sessions"],)):
        row = dict(zip(("local_session_id",) + sess_cols, r))
        pkg["new_sessions"].append(row)
    # 身份映射：按 updated_at 时间下限（uid 主键表，无自增边界可用）
    pkg["uname_map"] = [dict(zip(("uid", "uname", "updated_at"), r)) for r in
                        conn.execute("SELECT uid, uname, updated_at FROM uname_map "
                                     "WHERE updated_at >= ?", (stamp["ts_floor"],))]
    # 场次标题：首次或最近确认时间落在窗口内的（含旧场次窗口内的新标题）
    pkg["session_titles"] = [dict(zip(("session_id", "title", "first_seen", "last_seen"), r))
                             for r in conn.execute(
        "SELECT session_id, title, first_seen, last_seen FROM session_titles "
        "WHERE first_seen >= ? OR last_seen >= ?", (stamp["ts_floor"],) * 2)]
    return pkg


def cmd_export(args) -> int:
    if not STAMP_PATH.exists():
        print("[export] 未找到 stamp（data/backfill-stamp.json）。"
              "请先运行 snapshot 子命令。", file=sys.stderr)
        return 1
    stamp = json.loads(STAMP_PATH.read_text(encoding="utf-8"))
    conn = sqlite3.connect(f"file:{args.local_db}?mode=ro", uri=True)
    pkg = build_package(conn, stamp)
    conn.close()
    PACKAGE_PATH.write_text(json.dumps(pkg, ensure_ascii=False), encoding="utf-8")
    n_rows = {t: len(pkg["rows"][t]) for t in DETAIL_TABLES}
    print(f"[export] 回灌包已写入 {PACKAGE_PATH}")
    print(f"[export] 明细行数：{json.dumps(n_rows, ensure_ascii=False)}；"
          f"新建场次 {len(pkg['new_sessions'])} 个；"
          f"身份映射 {len(pkg['uname_map'])} 条；标题 {len(pkg['session_titles'])} 条")
    if not any(n_rows.values()) and not pkg["new_sessions"]:
        print("[export] 窗口内无新数据（本地端可能未采集或无事件）")
    return 0


# ---------------------------------------------------------------------------
# apply：把回灌包合并进服务器库（核心逻辑，本地/离线/测试共用）
# ---------------------------------------------------------------------------

def apply_core(db_path: Path | str, pkg: dict, tol: int = 5,
               session_match_tol: int = 600, dry_run: bool = False) -> dict:
    """执行合并，返回报告 dict。整个过程单事务（BEGIN IMMEDIATE 先取写锁，
    busy_timeout 30s），异常回滚——服务器采集进程可同时在线读写（WAL）。"""
    stamp = pkg["stamp"]
    maxids = stamp["max"]
    report: dict = {"tables": {}, "sessions": {"mapped": {}, "inserted": []},
                    "warnings": []}

    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")

        # ---- 1) 场次映射：窗口内本地新建场次 → 服务器场次 ----
        session_map: dict[int, int] = {}      # local session_id → server session_id
        inserted_server_ids: set[int] = set() # 以"插入"方式落位（计数不增量）
        for ls in pkg["new_sessions"]:
            best_sid, best_diff = None, None
            for cand in conn.execute(
                    "SELECT session_id, start_time FROM live_sessions "
                    "WHERE room_id = ? AND session_id > ?",
                    (ls["room_id"], maxids["live_sessions"])):
                d = abs(ts_diff_secs(ls["start_time"], cand["start_time"]))
                if best_diff is None or d < best_diff:
                    best_sid, best_diff = cand["session_id"], d
            if best_sid is not None and best_diff <= session_match_tol:
                session_map[ls["local_session_id"]] = best_sid
                report["sessions"]["mapped"][str(ls["local_session_id"])] = best_sid
                # 服务器侧若已结束而本地记录到更晚（重叠尾巴），延长 end_time
                if ls["end_time"] is not None:
                    cur = conn.execute("SELECT end_time FROM live_sessions "
                                       "WHERE session_id = ?", (best_sid,)).fetchone()
                    if cur and cur[0] is not None and ls["end_time"] > cur[0]:
                        conn.execute("UPDATE live_sessions SET end_time = ? "
                                     "WHERE session_id = ?",
                                     (ls["end_time"], best_sid))
            else:
                # 服务器没有对应场次（如整场直播都发生在重启窗口内）→ 插入新行，
                # 计数/峰值直接用本地值初始化（其明细全部会按新 id 导入）
                cur = conn.execute(
                    "INSERT INTO live_sessions (room_id, start_time, end_time, "
                    "peak_popularity, peak_viewers, danmaku_count, gift_value_yuan) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (ls["room_id"], ls["start_time"], ls["end_time"],
                     ls["peak_popularity"], ls["peak_viewers"],
                     ls["danmaku_count"], ls["gift_value_yuan"]))
                session_map[ls["local_session_id"]] = cur.lastrowid
                inserted_server_ids.add(cur.lastrowid)
                report["sessions"]["inserted"].append(
                    {"server_session_id": cur.lastrowid, "room_id": ls["room_id"],
                     "start_time": ls["start_time"]})

        # ---- 2) 明细表导入（边界去重 + 场次重映射）----
        # counter：映射/旧场次的增量计数与峰值（插入场次不重复累加）
        counter: dict[int, dict] = {}

        def _acc(sid: int, table: str, row: dict) -> None:
            c = counter.setdefault(sid, {"danmaku": 0, "gift": 0.0,
                                         "peak_pop": 0, "peak_view": 0})
            if table == "danmaku":
                c["danmaku"] += 1
            elif table == "gifts":
                c["gift"] += row["value_yuan"]
            elif table == "popularity_snapshots":
                c["peak_pop"] = max(c["peak_pop"], row["popularity"])
                c["peak_view"] = max(c["peak_view"], row["viewers"])

        for table in DETAIL_TABLES:
            rule = DEDUP_RULES[table]
            key_cols: list[str] = rule["keys"]
            cols: list[str] = pkg["columns"][table]
            imported = dup_skipped = dropped = 0

            # 先按精确键（含重映射后的场次）把本包行分组；无法归属场次的丢弃
            groups: dict[tuple, list[dict]] = {}
            for row in pkg["rows"].get(table, []):
                sid = row.get("session_id")
                if sid is None:
                    new_sid = None
                elif sid in session_map:
                    new_sid = session_map[sid]
                elif sid <= maxids["live_sessions"]:
                    new_sid = sid    # 旧场次：快照同源，服务器必然是同一 id
                    if not conn.execute("SELECT 1 FROM live_sessions "
                                        "WHERE session_id = ?", (sid,)).fetchone():
                        report["warnings"].append(
                            f"{table}: 行 ts={row['ts']} 引用的旧场次 #{sid} "
                            f"服务器缺失，丢弃")
                        dropped += 1
                        continue
                else:
                    report["warnings"].append(
                        f"{table}: 行 ts={row['ts']} 引用的本地新场次 #{sid} "
                        f"不在 new_sessions 列表中，丢弃")
                    dropped += 1
                    continue
                key = tuple(new_sid if k == "session_id" else row[k]
                            for k in key_cols)
                groups.setdefault(key, []).append({**row, "_sid": new_sid})

            # 组内双指针时间配对：把本地行与服务器候选（仅限快照后新写的行，
            # 即 id > 边界）各按时间排序后对齐推进，ts 差 ≤tol 判为同一事件
            # （重叠窗口两侧各记一条）——配对上的不导入；未配对的本地行才是
            # 真正要补的新事件。相比逐行 EXISTS，能正确处理"同键连发多条"
            # （连发间隔小于容差时不会被误吞）。
            for key, items in groups.items():
                items.sort(key=lambda r: r["ts"])
                where = [f"id > {maxids[table]}"] + [f"{k} IS ?" for k in key_cols]
                lo = ts_shift(items[0]["ts"], -tol)
                hi = ts_shift(items[-1]["ts"], tol)
                cands = [r[0] for r in conn.execute(
                    f"SELECT ts FROM {table} WHERE {' AND '.join(where)} "
                    f"AND ts BETWEEN ? AND ? ORDER BY ts",
                    list(key) + [lo, hi])]
                i = j = 0
                to_import: list[dict] = []
                while i < len(items) and j < len(cands):
                    diff = ts_diff_secs(items[i]["ts"], cands[j])
                    if -tol <= diff <= tol:
                        i += 1
                        j += 1        # 配对：重叠事件，服务器已有，跳过
                    elif diff < 0:
                        to_import.append(items[i])
                        i += 1
                    else:
                        j += 1        # 服务器独有（本地缺失窗口），推进候选
                to_import.extend(items[i:])
                dup_skipped += len(items) - len(to_import)
                for row in to_import:
                    vals = [row["_sid"] if c == "session_id" else row[c]
                            for c in cols]
                    conn.execute(f"INSERT INTO {table} ({','.join(cols)}) "
                                 f"VALUES ({','.join('?' * len(cols))})", vals)
                    imported += 1
                    if row["_sid"] is not None:
                        _acc(row["_sid"], table, row)
            report["tables"][table] = {"imported": imported,
                                       "dup_skipped": dup_skipped, "dropped": dropped}

        # ---- 3) 冗余计数与峰值增量（仅映射/旧场次；插入场次已在建行时初始化）----
        for sid, c in counter.items():
            if sid in inserted_server_ids:
                continue
            conn.execute(
                "UPDATE live_sessions SET "
                "danmaku_count = danmaku_count + ?, "
                "gift_value_yuan = round(gift_value_yuan + ?, 2), "
                "peak_popularity = MAX(peak_popularity, ?), "
                "peak_viewers = MAX(peak_viewers, ?) WHERE session_id = ?",
                (c["danmaku"], c["gift"], c["peak_pop"], c["peak_view"], sid))

        # ---- 4) 身份映射 upsert（仅当本地更新时间更新时覆盖昵称）----
        for m in pkg["uname_map"]:
            conn.execute(
                "INSERT INTO uname_map (uid, uname, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(uid) DO UPDATE SET uname = excluded.uname, "
                "updated_at = excluded.updated_at "
                "WHERE excluded.updated_at > uname_map.updated_at",
                (m["uid"], m["uname"], m["updated_at"]))
        report["uname_map"] = len(pkg["uname_map"])

        # ---- 5) 场次标题 upsert（重映射后按主键合并，时间取首末）----
        title_n = 0
        for t in pkg["session_titles"]:
            sid = t["session_id"]
            new_sid = session_map.get(sid, sid if sid <= maxids["live_sessions"]
                                      else None)
            if new_sid is None:
                continue
            conn.execute(
                "INSERT INTO session_titles (session_id, title, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(session_id, title) DO UPDATE SET "
                "first_seen = MIN(session_titles.first_seen, excluded.first_seen), "
                "last_seen = MAX(session_titles.last_seen, excluded.last_seen)",
                (new_sid, t["title"], t["first_seen"], t["last_seen"]))
            title_n += 1
        report["session_titles"] = title_n

        if dry_run:
            conn.rollback()
            report["dry_run"] = True
        else:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return report


def _print_report(rep: dict) -> None:
    for t, v in rep["tables"].items():
        print(f"[apply] {t}: 导入 {v['imported']}，重叠跳过 {v['dup_skipped']}，"
              f"丢弃 {v['dropped']}")
    for k, v in rep["sessions"]["mapped"].items():
        print(f"[apply] 场次映射：本地 #{k} → 服务器 #{v}")
    for s in rep["sessions"]["inserted"]:
        print(f"[apply] 新插入场次：服务器 #{s['server_session_id']} "
              f"（房间 {s['room_id']}，{s['start_time']} 开播）")
    print(f"[apply] 身份映射 {rep['uname_map']} 条，标题 {rep['session_titles']} 条")
    for w in rep["warnings"]:
        print(f"[apply] [警告] {w}")


def cmd_apply(args) -> int:
    pkg = json.loads(Path(args.package).read_text(encoding="utf-8"))
    if args.server_db:                       # 离线模式（测试 / 手工恢复）
        rep = apply_core(args.server_db, pkg, args.tol,
                         args.session_match_tol, args.dry_run)
        _print_report(rep)
        print("BACKFILL-OK" + (" (dry-run)" if args.dry_run else ""))
        return 0
    # 远程模式：把本脚本与回灌包传到服务器 /tmp，用服务器 python3 执行同一文件
    _require_host_args(args)
    key, host = Path(args.key), args.host
    remote_pkg = "/tmp/bili_backfill_package.json"
    subprocess.run(["scp", "-i", str(key), "-o", "BatchMode=yes",
                    str(Path(__file__).resolve()), f"{host}:{REMOTE_TOOL}"],
                   check=True, capture_output=True)
    subprocess.run(["scp", "-i", str(key), "-o", "BatchMode=yes",
                    str(args.package), f"{host}:{remote_pkg}"],
                   check=True, capture_output=True)
    r = run_ssh(["python3", REMOTE_TOOL, "_apply-on-server",
                 "--db", args.remote_db, "--package", remote_pkg,
                 "--tol", str(args.tol),
                 "--session-match-tol", str(args.session_match_tol)]
                + (["--dry-run"] if args.dry_run else []), key, host, check=False)
    print(r.stdout, end="")
    if r.stderr:
        print(r.stderr, end="", file=sys.stderr)
    run_ssh(["rm", "-f", REMOTE_TOOL, remote_pkg], key, host)  # 清理临时件
    if "BACKFILL-OK" not in r.stdout:
        print("[apply] 远程合并失败（详见上方输出），stamp 保留以便重试",
              file=sys.stderr)
        return 1
    if not args.dry_run:                     # 成功落盘后才消费 stamp，防重复回灌
        done = DATA_DIR / f"backfill-stamp.done-{now_iso().replace(':', '')}.json"
        STAMP_PATH.rename(done)
        print(f"[apply] stamp 已消费：{done.name}")
    return 0


def cmd_apply_on_server(args) -> int:
    """（内部）在服务器上被执行的入口：离线模式直连服务器库。"""
    pkg = json.loads(Path(args.package).read_text(encoding="utf-8"))
    rep = apply_core(args.db, pkg, args.tol, args.session_match_tol, args.dry_run)
    _print_report(rep)
    print("BACKFILL-REPORT " + json.dumps(rep, ensure_ascii=False))
    print("BACKFILL-OK" + (" (dry-run)" if args.dry_run else ""))
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="重启托底数据回灌工具（配合 AGENTS.md「服务器重启的数据保全规程」）",
        epilog="规程：snapshot → 启动本地托底采集 → 重启服务器 → 验证服务恢复 → "
               "export → apply → 向用户汇报")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("snapshot", help="拉取服务器一致快照到本地并记录边界 stamp")
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--key", default=str(DEFAULT_KEY))
    p.add_argument("--remote-db", default=DEFAULT_REMOTE_DB)
    p.add_argument("--local-db", default=str(LOCAL_DB))
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("export", help="打包本地边界之后的新数据")
    p.add_argument("--local-db", default=str(LOCAL_DB))
    p.add_argument("--package", default=str(PACKAGE_PATH))
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("apply", help="把回灌包合并进服务器库（默认远程；"
                                     "--server-db 指定文件则离线执行）")
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--key", default=str(DEFAULT_KEY))
    p.add_argument("--remote-db", default=DEFAULT_REMOTE_DB)
    p.add_argument("--package", default=str(PACKAGE_PATH))
    p.add_argument("--server-db", default=None,
                   help="离线模式：直接合并到该本地文件（测试/手工恢复用）")
    p.add_argument("--tol", type=int, default=5,
                   help="重叠去重的时间容差秒数（默认 5，容忍两机时钟偏差）")
    p.add_argument("--session-match-tol", type=int, default=600,
                   help="新场次映射的开播时间容差秒数（默认 600，与采集端 "
                        "session_merge_gap_sec 口径一致）")
    p.add_argument("--dry-run", action="store_true", help="只报告不落盘")
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("_apply-on-server", help=argparse.SUPPRESS)  # 内部：服务器端入口
    p.add_argument("--db", required=True)
    p.add_argument("--package", required=True)
    p.add_argument("--tol", type=int, default=5)
    p.add_argument("--session-match-tol", type=int, default=600)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_apply_on_server)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
