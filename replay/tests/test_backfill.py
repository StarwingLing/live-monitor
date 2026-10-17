# -*- coding: utf-8 -*-
"""v0.6.2 重启托底数据回灌工具测试（deploy/backfill_local_data.py）。

模拟规程全流程的真实数据库状态，验证合并核心逻辑：

1. 场景搭建：建"服务器库"（快照前数据）→ 复制为"本地库"（模拟 snapshot，
   此刻固定快照边界 stamp）→ 两库各自写入窗口期数据（本地托底采集 +
   服务器重启后恢复采集，含重叠窗口两侧各记一条的重复事件）；
2. build_package + apply_core 后断言：
   - 重叠事件不重复导入（弹幕/礼物/人气快照，含 NULL 场次与时钟偏差）；
   - 纯本地事件正确导入并归位到原场次（session_id 不变，不拆场）；
   - 窗口内本地新建场次按"同房间+开播时间接近"映射到服务器新场次（id 重映射）；
   - 服务器没有对应场次的本地新场（开播间隔 > 容差）插入为新场次，计数直接初始化；
   - 冗余计数增量、峰值取最大、end_time 不被误动；
   - uname_map 仅新时间覆盖、session_titles 按主键 upsert；
3. 幂等性：同一回灌包二次 apply，全部命中去重、无重复插入、计数不再变化；
4. dry-run：完整执行但回滚，库内容不变。

运行：uv run python tests/test_backfill.py
"""
import importlib.util
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from replay.db import SCHEMA  # noqa: E402  复用项目建表语句

# 以模块方式加载 deploy/ 下的工具脚本（不经过包导入，与服务器端执行方式一致）
_spec = importlib.util.spec_from_file_location(
    "backfill_local_data", PROJECT / "deploy" / "backfill_local_data.py")
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)

T = "2026-09-22T"
CHECKS = []


def check(name: str, cond: bool, detail: str = "") -> None:
    CHECKS.append((name, cond, detail))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"（{detail}）" if detail and not cond else ""))


def one(conn: sqlite3.Connection, sql: str, params=()):
    return conn.execute(sql, params).fetchone()[0]


def make_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA)
    return conn


def seed_server(conn: sqlite3.Connection) -> None:
    """快照前的服务器状态：3 个房间、1 个进行中场次（#1，房间 100）。"""
    for rid in (100, 200, 300):
        conn.execute("INSERT INTO rooms (room_id, created_at) VALUES (?, ?)",
                     (rid, T + "08:00:00"))
    conn.execute("INSERT INTO live_sessions (room_id, start_time) "
                 "VALUES (100, ?)", (T + "10:00:00",))
    # 快照前已有的一条弹幕 / 一条场外弹幕 / 两份身份映射 / 一条标题
    conn.execute("INSERT INTO danmaku (session_id, room_id, uid, uname, text, ts) "
                 "VALUES (1, 100, 1, '老观众', '开播弹幕', ?)", (T + "10:01:00",))
    conn.execute("INSERT INTO danmaku (session_id, room_id, uid, uname, text, ts) "
                 "VALUES (NULL, 100, 2, '路人', '场外弹幕', ?)", (T + "10:01:30",))
    conn.execute("INSERT INTO uname_map VALUES (1, '老用户', ?)", (T + "09:00:00",))
    conn.execute("INSERT INTO uname_map VALUES (5, '甲', ?)", (T + "10:30:00",))
    conn.execute("INSERT INTO session_titles VALUES (1, '标题A', ?, ?)",
                 (T + "09:59:00", T + "10:00:00"))
    conn.commit()


def window_server(conn: sqlite3.Connection) -> None:
    """重启后服务器恢复采集写的行（与本地端重叠的部分各记一条）。

    服务器侧还自行开了两场新直播：#2 房间300、#3 房间200。
    """
    # 重叠事件（本地端也会各记一条，时间略有偏差）
    conn.execute("INSERT INTO danmaku (session_id, room_id, uid, uname, text, ts) "
                 "VALUES (1, 100, 7, '重叠用户', '重叠消息', ?)", (T + "10:05:00",))
    conn.execute("INSERT INTO danmaku (session_id, room_id, uid, uname, text, ts) "
                 "VALUES (NULL, 100, 2, '路人', '场外重叠', ?)", (T + "10:05:30",))
    conn.execute("INSERT INTO gifts (session_id, room_id, uid, uname, type, name, "
                 "count, value_yuan, ts) VALUES (1, 100, 3, '送礼人', 'gift', "
                 "'辣条', 1, 0.1, ?)", (T + "10:06:00",))
    conn.execute("INSERT INTO popularity_snapshots (room_id, session_id, popularity, "
                 "viewers, ts) VALUES (100, 1, 120, 60, ?)", (T + "10:05:00",))
    # 服务器侧新场次：#2 房间300（与本地端稍后的 #3 是不同场，间隔超容差），
    # #3 房间200（与本地端稍后的 #2 是同一场，开播晚 30s）
    conn.execute("INSERT INTO live_sessions (room_id, start_time) "
                 "VALUES (300, ?)", (T + "10:08:00",))
    conn.execute("INSERT INTO live_sessions (room_id, start_time) "
                 "VALUES (200, ?)", (T + "10:10:30",))
    conn.execute("INSERT INTO danmaku (session_id, room_id, uid, uname, text, ts) "
                 "VALUES (3, 200, 9, '服务器侧用户', '服务器侧200房消息', ?)",
                 (T + "10:11:00",))
    # 模拟真实采集端 writer 的冗余计数自增（弹幕入明细时同步累加场次计数）
    conn.execute("UPDATE live_sessions SET danmaku_count = danmaku_count + 1 "
                 "WHERE session_id = 3")
    conn.commit()


def window_local(conn: sqlite3.Connection) -> None:
    """本地托底采集在窗口期写的行（快照后 id 递增，与服务器重叠的各一条）。"""
    D = ("INSERT INTO danmaku (session_id, room_id, uid, uname, text, ts) "
         "VALUES (?, ?, ?, ?, ?, ?)")
    # 重叠事件（时钟偏差 2 秒，应被去重跳过）
    conn.execute(D, (1, 100, 7, '重叠用户', '重叠消息', T + "10:04:58"))
    conn.execute(D, (None, 100, 2, '路人', '场外重叠', T + "10:05:28"))
    # 纯本地事件（重启窗口内只有本地在采集，应导入；连发两条同文不同秒）
    conn.execute(D, (1, 100, 8, '窗口用户', '仅本地消息', T + "10:06:00"))
    conn.execute(D, (1, 100, 8, '窗口用户', '仅本地消息', T + "10:06:03"))
    # 礼物：一条重叠辣条（跳过）+ 一条纯本地 SC（导入）
    conn.execute("INSERT INTO gifts (session_id, room_id, uid, uname, type, name, "
                 "text, count, value_yuan, ts) VALUES (1, 100, 3, '送礼人', 'gift', "
                 "'辣条', '', 1, 0.1, ?)", (T + "10:05:58",))
    conn.execute("INSERT INTO gifts (session_id, room_id, uid, uname, type, name, "
                 "text, count, value_yuan, ts) VALUES (1, 100, 10, 'SC用户', 'sc', "
                 "'SC', '加油', 1, 30.0, ?)", (T + "10:07:00",))
    # 人气快照：一条与服务器同一采样时刻（跳过）+ 一条独有（导入，带出峰值）
    conn.execute("INSERT INTO popularity_snapshots (room_id, session_id, popularity, "
                 "viewers, ts) VALUES (100, 1, 120, 61, ?)", (T + "10:04:58",))
    conn.execute("INSERT INTO popularity_snapshots (room_id, session_id, popularity, "
                 "viewers, ts) VALUES (100, 1, 130, 65, ?)", (T + "10:09:00",))
    conn.execute("INSERT INTO anchor_snapshots (room_id, attention, online, watched, "
                 "guards, ts) VALUES (100, 1005, 200, 3000, 12, ?)", (T + "10:09:30",))
    # 本地端新建场次 #2（房间 200，与服务器 #3 同一场）→ 应映射并重映射明细
    conn.execute("INSERT INTO live_sessions (room_id, start_time, peak_popularity, "
                 "peak_viewers, danmaku_count, gift_value_yuan) "
                 "VALUES (200, ?, 88, 44, 2, 0)", (T + "10:10:00",))
    conn.execute(D, (2, 200, 11, '用户甲', '200房消息1', T + "10:10:30"))
    conn.execute(D, (2, 200, 12, '用户乙', '200房消息2', T + "10:11:00"))
    # 本地端新建场次 #3（房间 300，距服务器 #2 开播 720s > 600s 容差）→ 应插入新场次
    conn.execute("INSERT INTO live_sessions (room_id, start_time, peak_popularity, "
                 "peak_viewers, danmaku_count, gift_value_yuan) "
                 "VALUES (300, ?, 50, 20, 1, 0)", (T + "10:20:00",))
    conn.execute(D, (3, 300, 13, '用户丙', '300房窗口场消息', T + "10:20:30"))
    # 身份映射（模拟采集端 upsert 行为）：uid1 更新（新于服务器 09:00 → 覆盖）；
    # uid2 新增；uid5 本地 10:09 旧于服务器 10:30 → 不得覆盖
    up = ("INSERT INTO uname_map VALUES (?, ?, ?) ON CONFLICT(uid) DO UPDATE SET "
          "uname = excluded.uname, updated_at = excluded.updated_at")
    conn.execute(up, (1, '老用户改名', T + "10:06:00"))
    conn.execute(up, (2, '新用户', T + "10:06:30"))
    conn.execute(up, (5, '乙', T + "10:09:00"))
    # 标题：旧场次窗口内新增标题 + 本地新场次的首标题
    conn.execute("INSERT INTO session_titles VALUES (1, '标题B', ?, ?)",
                 (T + "10:06:00",) * 2)
    conn.execute("INSERT INTO session_titles VALUES (2, '新标题', ?, ?)",
                 (T + "10:10:10", T + "10:12:00"))
    conn.commit()


def run_case(server_db: Path, local_db: Path, stamp: dict,
             dry_run: bool = False) -> dict:
    pkg_conn = sqlite3.connect(f"file:{local_db}?mode=ro", uri=True)
    pkg = tool.build_package(pkg_conn, stamp)
    pkg_conn.close()
    return tool.apply_core(server_db, pkg, dry_run=dry_run)


def verify_applied(conn: sqlite3.Connection, rep: dict) -> None:
    # ---- 明细导入与去重 ----
    check("重叠弹幕不重复（场次#1 内'重叠消息'恰 1 条）",
          one(conn, "SELECT count(*) FROM danmaku WHERE session_id=1 "
                    "AND uid=7 AND text='重叠消息'") == 1)
    check("重叠场外弹幕不重复（NULL 场次，IS 比较）",
          one(conn, "SELECT count(*) FROM danmaku WHERE session_id IS NULL "
                    "AND uid=2 AND text='场外重叠'") == 1)
    check("纯本地弹幕导入且连发两条都在（同文不同秒不去重）",
          one(conn, "SELECT count(*) FROM danmaku WHERE session_id=1 "
                    "AND uid=8 AND text='仅本地消息'") == 2)
    check("重叠礼物跳过、本地 SC 导入",
          one(conn, "SELECT count(*) FROM gifts WHERE uid=3 AND name='辣条'") == 1
          and one(conn, "SELECT count(*) FROM gifts WHERE uid=10 "
                        "AND type='sc'") == 1)
    check("人气快照去重一条、导入一条",
          one(conn, "SELECT count(*) FROM popularity_snapshots "
                    "WHERE ts=?", (T + "10:09:00",)) == 1
          and one(conn, "SELECT count(*) FROM popularity_snapshots "
                        "WHERE ts BETWEEN ? AND ?",
                  (T + "10:04:53", T + "10:05:03")) == 1)
    check("主播快照导入",
          one(conn, "SELECT count(*) FROM anchor_snapshots WHERE ts=?",
              (T + "10:09:30",)) == 1)
    # ---- 场次映射与插入 ----
    check("本地新场次#2 映射到服务器#3（200 房，开播差 30s）",
          rep["sessions"]["mapped"].get("2") == 3)
    check("200 房消息重映射进服务器#3",
          one(conn, "SELECT count(*) FROM danmaku WHERE session_id=3 "
                    "AND text LIKE '200房消息%'") == 2)
    ins = rep["sessions"]["inserted"]
    check("300 房窗口场插入为新场次（间隔 720s > 600s 容差）",
          len(ins) == 1 and ins[0]["room_id"] == 300)
    sid4 = ins[0]["server_session_id"] if ins else -1
    check("300 房消息归位到插入的新场次",
          one(conn, "SELECT count(*) FROM danmaku WHERE session_id=? "
                    "AND text='300房窗口场消息'", (sid4,)) == 1)
    # ---- 计数 / 峰值 / end_time ----
    s1 = conn.execute("SELECT danmaku_count, gift_value_yuan, peak_popularity, "
                      "peak_viewers, end_time FROM live_sessions "
                      "WHERE session_id=1").fetchone()
    check("场次#1 计数增量（+2 弹幕 +30 元）峰值取最大、end_time 未动",
          s1[0] == 2 and abs(s1[1] - 30.0) < 1e-9 and s1[2] == 130
          and s1[3] == 65 and s1[4] is None)
    s3 = one(conn, "SELECT danmaku_count FROM live_sessions WHERE session_id=3")
    check("映射场次#3 计数增量（服务器 1 + 导入 2 = 3）", s3 == 3)
    s4 = conn.execute("SELECT danmaku_count, peak_popularity, end_time "
                      "FROM live_sessions WHERE session_id=?",
                      (sid4,)).fetchone()
    check("插入场次计数直接初始化（1 条弹幕、峰值 50、未结束）",
          s4[0] == 1 and s4[1] == 50 and s4[2] is None)
    # ---- 身份映射 / 标题 ----
    check("uname_map 新时间覆盖、旧时间不覆盖、新用户插入",
          one(conn, "SELECT uname FROM uname_map WHERE uid=1") == '老用户改名'
          and one(conn, "SELECT uname FROM uname_map WHERE uid=5") == '甲'
          and one(conn, "SELECT uname FROM uname_map WHERE uid=2") == '新用户')
    check("标题 upsert：旧场次新标题 + 新场次标题随映射归位",
          one(conn, "SELECT count(*) FROM session_titles WHERE session_id=1 "
                    "AND title='标题B'") == 1
          and one(conn, "SELECT count(*) FROM session_titles WHERE session_id=3 "
                        "AND title='新标题'") == 1)
    # ---- 报告数字 ----
    check("报告计数与断言一致",
          rep["tables"]["danmaku"] == {"imported": 5, "dup_skipped": 2,
                                       "dropped": 0}
          and rep["tables"]["gifts"]["imported"] == 1
          and rep["tables"]["popularity_snapshots"] == {
              "imported": 1, "dup_skipped": 1, "dropped": 0})


def main() -> int:
    with tempfile.TemporaryDirectory(dir=str(PROJECT),
                                     ignore_cleanup_errors=True) as td:
        tdp = Path(td)
        # ---- 搭建：服务器库 → 复制为本地库（此刻固定快照边界）→ 双方窗口写入 ----
        server_db, local_db = tdp / "server.db", tdp / "local.db"
        conn = make_db(server_db)
        seed_server(conn)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # 快照前并 WAL，拷贝干净
        conn.close()
        shutil.copyfile(server_db, local_db)             # snapshot（快照点）
        snap_conn = sqlite3.connect(f"file:{local_db}?mode=ro", uri=True)
        stamp = {"tool": "backfill_local_data", "version": 1,
                 "created_at": T + "10:03:00", "ts_floor": T + "09:59:00",
                 "max": tool.collect_boundaries(snap_conn)}
        snap_conn.close()
        c = make_db(local_db)
        window_local(c)
        c.close()
        conn = make_db(server_db)                        # 复用已建的库续写窗口数据
        window_server(conn)
        conn.close()

        # ---- 1) dry-run：报告完整但库不变 ----
        print("== dry-run：")
        rep = run_case(server_db, local_db, stamp, dry_run=True)
        conn = sqlite3.connect(f"file:{server_db}?mode=ro", uri=True)
        for tbl, n in {"danmaku": 5, "gifts": 1, "popularity_snapshots": 1,
                       "live_sessions": 3}.items():
            check(f"dry-run 后 {tbl} 行数不变（{n}）",
                  one(conn, f"SELECT count(*) FROM {tbl}") == n)
        check("dry-run 标记存在", rep.get("dry_run") is True)
        check("dry-run 报告仍给出将导入的数字",
              rep["tables"]["danmaku"]["imported"] == 5)
        conn.close()

        # ---- 2) 正式 apply ----
        print("== apply：")
        rep = run_case(server_db, local_db, stamp)
        conn = sqlite3.connect(f"file:{server_db}?mode=ro", uri=True)
        verify_applied(conn, rep)
        n_danmaku = one(conn, "SELECT count(*) FROM danmaku")
        s1_cnt = one(conn, "SELECT danmaku_count FROM live_sessions "
                           "WHERE session_id=1")
        conn.close()

        # ---- 3) 幂等：同一包再灌一次，全部命中去重、无新增、计数不变 ----
        print("== 幂等性（二次 apply 同一包）：")
        rep2 = run_case(server_db, local_db, stamp)
        conn = sqlite3.connect(f"file:{server_db}?mode=ro", uri=True)
        check("二次 apply 弹幕零导入（包内 7 条全部命中去重）",
              rep2["tables"]["danmaku"]["imported"] == 0
              and rep2["tables"]["danmaku"]["dup_skipped"] == 7)
        check("二次 apply 无重复插入场次", rep2["sessions"]["inserted"] == [])
        check("二次 apply 后行数与计数不变",
              one(conn, "SELECT count(*) FROM danmaku") == n_danmaku
              and one(conn, "SELECT danmaku_count FROM live_sessions "
                            "WHERE session_id=1") == s1_cnt)
        conn.close()

        # ---- 4) 工具函数：时间平移 ----
        print("== 工具函数：")
        check("ts_shift 平移正确（含跨分钟进位）",
              tool.ts_shift(T + "10:05:00", -2) == T + "10:04:58"
              and tool.ts_shift(T + "10:59:58", 5) == T + "11:00:03")

    failed = [c for c in CHECKS if not c[1]]
    print(f"\n共 {len(CHECKS)} 项，通过 {len(CHECKS) - len(failed)}，失败 {len(failed)}")
    for name, _, detail in failed:
        print(f"  FAIL  {name} {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
