# -*- coding: utf-8 -*-
"""转码 + 上云 + 归档 + 本地释放管线（v2.2 自 bili-monitor web app 迁入）。

单线程 daemon worker（replay-web lifespan 内启动，先睡满一个扫描周期再扫
——v1.4 OOM 崩溃循环事故的防线）每 [oss].scan_interval_sec（默认 10 分钟）
执行 _scan_pass() 一轮：

1. 磁盘压力快路径（v2.1）：剩余 < [recording].emergency_free_gb 时优先
   上云最早片段并清分段 / 请求提前轮换；
2. 清扫：孤儿 low 目录 / 超时 .part 墓碑；
3. 转码：缺 low 的终态录播与手动切片 → 480p/700k 单线程副本（.part 原子
   改名，失败留墓碑 2h 退避）；
4. 上云（v2.1 全量 mid 热桶）：录播原片/low、切片及 low 全落
   [oss].clips_bucket（Standard），行级桶列回填；
5. 归档：录播原片 mid 存满 [oss].rec_archive_after_days（10 天）由
   Copier 服务端复制主桶 IA、删 mid 副本（主桶已有则只删 mid）；
6. 释放（v2.1 零留存）：录播原片+low 双上云即删本地；切片本体与 low 按
   clips_keep_hours / clips_low_keep_hours（服务器配置 0 = 上云即释）。

高能成片分支已随 v2.2 高能功能移除。OSS 全程静默降级：enabled=false 或
AK 异常时上云/归档分支跳过，系统回到纯本地行为。
"""
import shutil
import subprocess
import time
from pathlib import Path

from ..config import DATA_DIR, get_config
from ..db import connect, now_iso
from ..capture import recording as rec_mod

# 录像根目录经模块引用晚绑定（测试重绑 replay.capture.recording.
# RECORDINGS_ROOT 后本模块同步生效；不宜 from-import 早绑定值）

cfg = get_config()

# ---------------------------------------------------------------------------
# OSS 客户端管理（内网上传/删除 + 公网签名双客户端懒加载）
# ---------------------------------------------------------------------------
_OSS_CLIENTS: dict[str, object] = {}


def oss_enabled() -> bool:
    """OSS 总开关（config [oss].enabled 且 bucket/AK 齐备）。"""
    o = cfg.get("oss", {})
    return bool(o.get("enabled") and o.get("bucket")
                and o.get("access_key_id") and o.get("access_key_secret"))


def _oss_client(internal: bool):
    """懒加载 OSS 客户端（线程内串行使用，无需加锁）。

    internal=True → 上传用内网 endpoint（ECS 同地域免流量费）；
    internal=False → 签名用公网 endpoint（内网域名浏览器不可解析）。
    初始化失败返回 None（调用方静默跳过，系统回落纯本地行为）。
    """
    key = "in" if internal else "pub"
    if key in _OSS_CLIENTS:
        return _OSS_CLIENTS[key]
    o = cfg.get("oss", {})
    try:
        import alibabacloud_oss_v2 as oss
        c = oss.config.load_default()
        c.region = o.get("region", "cn-hangzhou")
        c.endpoint = o["endpoint"] if internal else \
            f"oss-{c.region}.aliyuncs.com"
        c.credentials_provider = oss.credentials.StaticCredentialsProvider(
            access_key_id=o["access_key_id"],
            access_key_secret=o["access_key_secret"])
        _OSS_CLIENTS[key] = oss.Client(c)
    except Exception:
        _OSS_CLIENTS[key] = None
    return _OSS_CLIENTS[key]


def _oss_key_of(rel_path: str) -> str:
    """本地相对路径 → OSS 对象 key（recordings/ 前缀 + 同构路径）。"""
    return "recordings/" + rel_path.replace("\\", "/")


def _oss_low_key_of(rel_path: str) -> str:
    """原片相对路径 → 低画质副本对象 key（<stem>/low.mp4）。"""
    return _oss_key_of(rel_path[:-4] + "/low.mp4")


def _clips_bucket() -> str:
    """全量热桶（v2.1：[oss].clips_bucket 语义为所有视频的处理完成落点；
    未配置时回落主桶保底可用）。"""
    return cfg.get("oss", {}).get("clips_bucket") or cfg["oss"]["bucket"]


def _oss_upload(local_path: Path, key: str, standard: bool,
                bucket: str = "") -> bool:
    """上传单文件（分片断点）；standard=True 显式标准存储（播放对象），
    False 落 bucket 默认低频 IA（原片归档）；bucket 空串 = 主桶。
    成功 True。"""
    client = _oss_client(internal=True)
    if not client:
        return False
    import alibabacloud_oss_v2 as oss
    # storage_class 为 SDK 正式参数（headers 传法不生效——首载实测教训：
    # 13 条切片落成 bucket 默认 IA 后靠 copy_object 自拷转回 Standard）
    req = oss.models.object_basic.PutObjectRequest(
        bucket=bucket or cfg["oss"]["bucket"], key=key,
        storage_class="Standard" if standard else "IA")
    up = oss.Uploader(client)
    up.upload_file(req, filepath=str(local_path), part_size=1 << 20,
                   parallel_num=2, enable_checkpoint=True,
                   checkpoint_dir=str(DATA_DIR / "oss_checkpoints"))
    return True


def _oss_delete(key: str, bucket: str = "") -> None:
    """删除云端对象（尽力而为，失败只记日志——对应用户主动删除联动）。
    bucket 空串 = 主桶；切片对象需显式传其上云 bucket。"""
    client = _oss_client(internal=True)
    if not client:
        return
    import alibabacloud_oss_v2 as oss
    try:
        client.delete_object(oss.models.object_basic.DeleteObjectRequest(
            bucket=bucket or cfg["oss"]["bucket"], key=key))
    except Exception:
        pass


def _oss_exists(key: str, bucket: str = "") -> bool:
    """HEAD 校验对象是否存在（v2.1 迁移/归档用；404 或异常一律 False）。"""
    client = _oss_client(internal=True)
    if not client or not key:
        return False
    import alibabacloud_oss_v2 as oss
    try:
        client.head_object(oss.models.object_basic.HeadObjectRequest(
            bucket=bucket or cfg["oss"]["bucket"], key=key))
        return True
    except Exception:
        return False


def _oss_copy(key: str, src_bucket: str, dst_bucket: str,
              storage_class: str = "") -> bool:
    """v2.1 服务端跨桶复制（同区域内网，免流量费；Copier 对大对象自动走
    分片复制——CopyObject 原生接口 5GB 上限，10G 级录播必须 Copier）。
    storage_class 空 = 目标桶默认类型；归档主桶显式传 'IA'。
    已通过 context7 核对（alibabacloud-oss-python-sdk-v2 DEVGUIDE/Copier 文档）；
    装机 SDK 1.4.0 实测方法名为 Copier.copy（文档写的 copy_object 与 1.4.0
    不符，AttributeError 实证）。"""
    client = _oss_client(internal=True)
    if not client:
        return False
    import alibabacloud_oss_v2 as oss
    try:
        req = oss.models.object_basic.CopyObjectRequest(
            bucket=dst_bucket, key=key,
            source_bucket=src_bucket, source_key=key)
        if storage_class:
            req.storage_class = storage_class
        oss.Copier(client).copy(req)
        return True
    except Exception as e:
        print(f"[oss] copy {src_bucket}->{dst_bucket}/{key} 失败: {e!r}")
        return False


# ---------------------------------------------------------------------------
# 查询小助手（与 web/app 的 q/q1 同语义；本模块自备避免循环 import）
# ---------------------------------------------------------------------------
def q(sql: str, params: tuple = ()) -> list:
    """只读查询（短连接，返回全部行）。"""
    conn = connect()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def q1(sql: str, params: tuple = ()):
    """只读查询单行（无行返回 None）。"""
    rows = q(sql, params)
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# 转码
# ---------------------------------------------------------------------------
def _part_tombstone(part: Path) -> None:
    """转码失败时把 .part 截成 0 字节并刷新 mtime（v1.4 防崩溃循环）。

    .part 存在即跳过重试、超 7200s 由清扫轮删除——失败墓碑把「10 分钟一轮
    的无界重试」变成「至多 2 小时一次」。这对 2026-10-08 的事故是关键保险：
    当时 ffmpeg 撞 MemoryMax=200M 被 cgroup OOM 杀死（转码本身也是被杀方，
    走不到这里的清理分支，.part 天然残留成墓碑），systemd 13 秒一轮快速
    重启 + worker 启动即扫，形成 13 小时 3000+ 次重启的紧循环；现在
    worker 先睡满一个扫描周期再扫（见 _low_quality_worker），两道防线
    叠加后持续失败的转码至多 2 小时打扰一次，且不再拖死 API 服务。"""
    try:
        part.write_text("")
        ts = time.time()
        import os as _os
        _os.utime(part, (ts, ts))
    except OSError:
        try:
            part.unlink(missing_ok=True)   # 截断也失败（只读卷等）→ 删除兜底
        except OSError:
            pass


def _make_low(src: Path, dst: Path) -> None:
    """单线程转码 480p/700k 低清副本（v1.5 起录播成片与手动切片共用）。

    产物先写 .part、成功后原子改名；失败留 .part 墓碑（2h 退避重试，
    v1.4 OOM 教训的防线在此复用）。单线程 + veryfast 约 3~6 倍实时：
    2C 小机上 x264 默认多线程会把负载打到 7~9，拖慢采集端弹幕处理，
    nice 只能让出调度优先级挡不住负载本身。"""
    part = dst.with_name(dst.name + ".part")
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["nice", "-n", "19", "ffmpeg", "-y", "-filter_threads", "1",
           "-i", str(src), "-vf", "scale=-2:480", "-c:v", "libx264",
           "-preset", "veryfast", "-threads", "1",
           "-b:v", "700k", "-maxrate", "900k",
           "-bufsize", "1800k", "-c:a", "aac", "-b:a", "96k",
           "-movflags", "+faststart", "-f", "mp4", str(part)]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=7200)
        if p.returncode == 0 and part.exists():
            part.replace(dst)
        else:
            _part_tombstone(part)
    except Exception:
        _part_tombstone(part)


# ---------------------------------------------------------------------------
# 磁盘压力快路径（v2.1）
# ---------------------------------------------------------------------------
def _disk_pressure_fastpath() -> None:
    """v2.1 磁盘压力快路径（用户 2026-10-12 定稿规则）：worker 每轮扫描首步
    检查，剩余空间 < [recording].emergency_free_gb（默认 5GB）时——
    ① 本地仍有分段的最早已完成片段：原片未上云先优先上云（跳过转码排队），
      确认 oss_key 后删除其分段（本体原片留待 low 完成、双键齐备后由零
      留存释放分支清理）；
    ② 仍不足且存在进行中的片段（其分段无法先上云——成片还没拼出来）：
      置 rotate_requested，capture 2s 扫描即提前轮换（旧段拼接→上云→
      清分段，走 ① 同口径）。"""
    if not oss_enabled():
        return
    rcfg = cfg.get("recording", {})
    free_min = float(rcfg.get("emergency_free_gb", 5.0)) * 1e9
    try:
        free = shutil.disk_usage(DATA_DIR).free
    except OSError:
        return
    if free >= free_min:
        return
    print(f"[maintenance] 磁盘剩余 {free / 1e9:.1f}GB 低于 "
          f"{free_min / 1e9:.0f}GB，压力快路径启动")
    hot = _clips_bucket()
    conn = connect()
    try:
        # ① 已完成片段的滞留分段（正常流拼接成功即删；此处兜拼接中断等遗留）
        rows = conn.execute(
            "SELECT id, room_id, file_path, oss_key, oss_low_key "
            "FROM recordings WHERE status IN ('finished','interrupted') "
            "AND file_path LIKE '%.mp4' ORDER BY ended_at ASC, id ASC"
        ).fetchall()
        for r in rows:
            if free >= free_min:
                break
            stem = rec_mod.RECORDINGS_ROOT / r["file_path"][:-4]
            segs = sorted(stem.glob("seg_*.ts")) if stem.is_dir() else []
            if not segs:
                continue
            if not r["oss_key"]:
                src = rec_mod.RECORDINGS_ROOT / r["file_path"]
                if not src.exists():
                    continue
                if not _oss_upload(src, _oss_key_of(r["file_path"]),
                                   standard=True, bucket=hot):
                    continue          # 上不去云：本行动不了，跳过
                conn.execute(
                    "UPDATE recordings SET oss_key = ?, oss_bucket = ?, "
                    "oss_done_at = ? WHERE id = ?",
                    (_oss_key_of(r["file_path"]), hot, now_iso(), r["id"]))
                conn.commit()
            freed = 0
            for f in segs:
                try:
                    freed += f.stat().st_size
                    f.unlink()
                except OSError:
                    pass
            free += freed
            print(f"[maintenance] 压力快路径：已上云 rec #{r['id']} 分段清理"
                  f"（{len(segs)} 段 {freed / 1e9:.2f}GB）")
        # ② 仍不足：进行中的最早片段提前轮换（旧段成片后分段即释放源）
        if free < free_min:
            row = conn.execute(
                "SELECT id FROM recordings WHERE status = 'recording' "
                "ORDER BY started_at ASC LIMIT 1").fetchone()
            if row:
                cur = conn.execute(
                    "UPDATE recordings SET status = 'rotate_requested' "
                    "WHERE id = ? AND status = 'recording'",
                    (row["id"],))
                conn.commit()
                if cur.rowcount:
                    print(f"[maintenance] 压力快路径：请求提前轮换 "
                          f"rec #{row['id']}")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 单轮扫描体（独立成函数便于测试直接调用）
# ---------------------------------------------------------------------------
def _scan_pass() -> None:
    """转码+上云线程的单轮扫描体。

    依次：1. 孤儿 low 目录/过期 .part 清扫 + 缺 low 的终态成片与手动切片
    单线程转码（v1.1 录播、v1.5 切片同口径）；2. low 版上传（转出即传，
    Standard）+ 原片/切片上传（终态即传，全量 mid 热桶）→ 成功写
    oss_key/oss_done_at；3. 录播原片 10 天归档主桶 IA；4. 本地释放（v2.1
    零留存：录播原片+low 双上云即删；切片按 clips_keep_hours /
    clips_low_keep_hours）。未上云的文件永不释放（不丢数据）。
    enabled=false 或 AK 异常 → 上云分支静默跳过（纯本地行为）。
    """
    ocfg = cfg.get("oss", {})
    # v2.1：压力快路径先行（磁盘 <5GB 时优先上云最早片段并清分段/请求
    # 提前轮换——转码一跑就是几十分钟，不能等它让位）
    _disk_pressure_fastpath()
    keep_full_h = float(ocfg.get("full_keep_hours", 96))
    restore_keep_h = float(ocfg.get("restore_keep_hours", 24))
    keep_clips_h = float(ocfg.get("clips_keep_hours", 120))
    clips_low_keep_h = float(ocfg.get("clips_low_keep_hours", 720))
    if True:
        try:
            # ---- 清扫（v1.1；v1.5 补三级路径——切片 low 目录比录播深一级：
            #      录播 <房>/full_<id>/low.mp4 两级、切片 <房>/manual/clip_<id>/
            #      low.mp4 三级）----
            # 守卫（2026-10-11）：本体「不在盘」在零留存策略下是常态（上云即
            # 释），不能再当孤儿整删——切片 low 有 DB 行且已上云 → 在预览
            # 窗口内（释放分支独立计时收），保留；录播 low 未上云
            # （oss_low_key 空）而原片已释放 → 本地是唯一副本，保留给上传
            # 重试，只有已上云的残留 low 才可收
            for low in rec_mod.RECORDINGS_ROOT.glob("*/*/low.mp4"):
                src = low.parent.parent / (low.parent.name + ".mp4")
                if src.exists():
                    continue
                orig_rel = (str(low.parent.parent.relative_to(rec_mod.RECORDINGS_ROOT))
                            .replace("\\", "/") + "/" + low.parent.name + ".mp4")
                row = q1("SELECT oss_key, oss_low_key FROM recordings "
                         "WHERE file_path = ? LIMIT 1", (orig_rel,))
                if row and row["oss_key"] and not row["oss_low_key"]:
                    continue   # 未上云 low：原片已释，本地即唯一副本
                shutil.rmtree(low.parent, ignore_errors=True)
            for low in rec_mod.RECORDINGS_ROOT.glob("*/*/*/low.mp4"):
                src = low.parent.parent / (low.parent.name + ".mp4")
                if src.exists():
                    continue
                orig_rel = (str(low.parent.parent.relative_to(rec_mod.RECORDINGS_ROOT))
                            .replace("\\", "/") + "/" + low.parent.name + ".mp4")
                row = q1("SELECT oss_key FROM manual_clips "
                         "WHERE file_path = ? LIMIT 1", (orig_rel,))
                if row and row["oss_key"]:
                    continue   # 本体按策略释放：low 在预览窗口内
                shutil.rmtree(low.parent, ignore_errors=True)
            for part in rec_mod.RECORDINGS_ROOT.glob("*/*/low.mp4.part"):
                try:
                    if time.time() - part.stat().st_mtime > 7200:
                        part.unlink()
                except OSError:
                    pass
            for part in rec_mod.RECORDINGS_ROOT.glob("*/*/*/low.mp4.part"):
                try:
                    if time.time() - part.stat().st_mtime > 7200:
                        part.unlink()
                except OSError:
                    pass
            # ---- 转码（v1.1 录播、v1.5 切片；一次一个，_make_low 单线程）----
            rows = q("SELECT id, room_id, file_path, oss_key, oss_low_key "
                     "FROM recordings "
                     "WHERE status IN ('finished','interrupted') "
                     "AND file_path LIKE '%.mp4' ORDER BY id DESC")
            for r in rows:
                src = rec_mod.RECORDINGS_ROOT / r["file_path"]
                if not src.exists():
                    continue
                if r["oss_low_key"]:
                    # v2.0.1：low 已在云端（原片释放后回灌的场景）——本地
                    # low 的意义就是上云，已上云再转是纯浪费（10G 原片在
                    # 2C 小机上 ~1h CPU）；跳过，播放链自有 OSS low 兜底
                    continue
                dst_dir = rec_mod.RECORDINGS_ROOT / r["file_path"][:-4]
                dst, part = dst_dir / "low.mp4", dst_dir / "low.mp4.part"
                if dst.exists() or part.exists():
                    continue
                _make_low(src, dst)
            # 手动切片 low（v1.5：切片与录播同款 480p 低清预览。回放页本地
            # 播放本就先试 <stem>/low.mp4、404 回退原片——此前后端从未给
            # 切片生产 low，前端该请求恒 404；本分支补齐后存量 done 切片
            # 由扫描自动回填转码）
            rows = q("SELECT id, file_path FROM manual_clips "
                     "WHERE status='done' AND file_path LIKE '%.mp4' "
                     "ORDER BY id DESC")
            for r in rows:
                src = rec_mod.RECORDINGS_ROOT / r["file_path"]
                if not src.exists():
                    continue
                dst_dir = rec_mod.RECORDINGS_ROOT / r["file_path"][:-4]
                dst, part = dst_dir / "low.mp4", dst_dir / "low.mp4.part"
                if dst.exists() or part.exists():
                    continue
                _make_low(src, dst)
            # ---- 上云（v1.2 起）：low 即传 + 原片/切片即传 ----
            # 任何一文件失败只跳过，下轮重试；OSS 全程静默降级
            if oss_enabled():
                conn = connect()
                try:
                    # 整场成片（v2.1：原片与 low 都落 mid 热桶 Standard——
                    # 10 天热窗口内点播走标准桶；原片到期由下方归档步骤
                    # 服务端复制回主桶 IA，mid 副本即释）
                    hot = _clips_bucket()
                    for r in conn.execute(
                            "SELECT id, file_path, oss_key, oss_low_key "
                            "FROM recordings WHERE status IN "
                            "('finished','interrupted') "
                            "AND file_path LIKE '%.mp4'").fetchall():
                        src = rec_mod.RECORDINGS_ROOT / r["file_path"]
                        low_src = rec_mod.RECORDINGS_ROOT / (r["file_path"][:-4]
                                                     + "/low.mp4")
                        up = {}
                        if not r["oss_low_key"] and low_src.exists():
                            if _oss_upload(low_src,
                                           _oss_low_key_of(r["file_path"]),
                                           standard=True, bucket=hot):
                                up["oss_low_key"] = _oss_low_key_of(
                                    r["file_path"])
                                up["oss_low_bucket"] = hot
                        if not r["oss_key"] and src.exists():
                            if _oss_upload(src, _oss_key_of(r["file_path"]),
                                           standard=True, bucket=hot):
                                up["oss_key"] = _oss_key_of(r["file_path"])
                                up["oss_bucket"] = hot
                                up["oss_done_at"] = now_iso()
                        if up:
                            sets = ", ".join(f"{k} = ?" for k in up)
                            conn.execute(
                                f"UPDATE recordings SET {sets} WHERE id = ?",
                                (*up.values(), r["id"]))
                            conn.commit()
                    # 手动切片（Standard；v1.4 起本体上 [oss].clips_bucket
                    # 独立桶；v1.5 起 low 副本同桶即传——切片与 low 都是
                    # 高频播放对象，本体与 low 各自独立判传）
                    for r in conn.execute(
                            "SELECT id, file_path, oss_key, oss_low_key "
                            "FROM manual_clips "
                            "WHERE status='done' AND file_path != '' "
                            "AND file_path LIKE '%.mp4'").fetchall():
                        clip_bucket = _clips_bucket()
                        up = {}
                        low_src = rec_mod.RECORDINGS_ROOT / (r["file_path"][:-4]
                                                     + "/low.mp4")
                        if not r["oss_low_key"] and low_src.exists():
                            if _oss_upload(low_src,
                                           _oss_low_key_of(r["file_path"]),
                                           standard=True,
                                           bucket=clip_bucket):
                                up["oss_low_key"] = _oss_low_key_of(
                                    r["file_path"])
                        if not r["oss_key"] and (rec_mod.RECORDINGS_ROOT
                                                 / r["file_path"]).exists():
                            if _oss_upload(rec_mod.RECORDINGS_ROOT / r["file_path"],
                                           _oss_key_of(r["file_path"]),
                                           standard=True, bucket=clip_bucket):
                                up["oss_key"] = _oss_key_of(r["file_path"])
                                up["oss_bucket"] = clip_bucket
                                up["oss_done_at"] = now_iso()
                        if up:
                            sets = ", ".join(f"{k} = ?" for k in up)
                            conn.execute(
                                f"UPDATE manual_clips SET {sets} WHERE id = ?",
                                (*up.values(), r["id"]))
                            conn.commit()
                    # ---- v2.1 录播原片 10 天归档：mid → 主桶（IA）----
                    # 主桶已有副本（存量迁移行——迁移只复制不删主桶）则跳过
                    # 复制只删 mid 副本；low 不归档，永久留 mid 供回放点播。
                    # 失败本轮跳过，下轮重试（幂等：HEAD→copy→delete→翻行）。
                    archive_days = float(ocfg.get("rec_archive_after_days", 10))
                    if archive_days > 0:
                        rows = conn.execute(
                            "SELECT id, file_path, oss_key, oss_bucket "
                            "FROM recordings WHERE oss_bucket != '' "
                            "AND oss_key != '' AND oss_done_at != '' "
                            "AND julianday(oss_done_at) <= "
                            "julianday('now', ?)",
                            (f"-{archive_days} days",)).fetchall()
                        for r in rows:
                            if not _oss_exists(r["oss_key"]):
                                if not _oss_copy(r["oss_key"],
                                                 r["oss_bucket"],
                                                 cfg["oss"]["bucket"], "IA"):
                                    continue
                            _oss_delete(r["oss_key"], bucket=r["oss_bucket"])
                            conn.execute(
                                "UPDATE recordings SET oss_bucket = '' "
                                "WHERE id = ?", (r["id"],))
                            conn.commit()
                            print(f"[oss] 录播归档 rec #{r['id']} → 主桶"
                                  f"（mid 副本 {r['file_path']} 已释）")
                    # 本地释放（用户留存规则）：已上云 且 mtime 超时 → 删。
                    # v2.2.1 回灌窗口独立计时：restored_at 非空 = 本地原片是
                    # 回灌产物（服务切片动线），按 restore_keep_hours（默认
                    # 24h）评估；否则按 full_keep_hours（服务器 0 = 上云即释）
                    # 原片 + 已上云 low 一起评估（原片释放时 low 同随）
                    # v2.1 零留存守卫：原片+low **双上云**才释放本地原片——
                    # 只看 oss_key 会在 low 尚未转码时把源片删掉，low 从此
                    # 造不出来（low 的源是本地原片）。low 转码持续失败时
                    # 原片留盘（数据安全方向），压力快路径/紧急清理兜底。
                    for r in conn.execute(
                            "SELECT id, room_id, file_path, oss_key, "
                            "oss_low_key, restored_at FROM recordings "
                            "WHERE status IN "
                            "('finished','interrupted') "
                            "AND file_path LIKE '%.mp4'").fetchall():
                        if not (r["oss_key"] and r["oss_low_key"]):
                            continue   # 未上云（或 low 未就绪）永不释放
                        src = rec_mod.RECORDINGS_ROOT / r["file_path"]
                        if not src.exists():
                            continue
                        keep_h = restore_keep_h if r["restored_at"] else keep_full_h
                        try:
                            if src.stat().st_mtime > time.time() - keep_h * 3600:
                                continue   # 留存窗口内
                        except OSError:
                            continue
                        src.unlink(missing_ok=True)
                        if r["restored_at"]:
                            conn.execute(
                                "UPDATE recordings SET restored_at = '' "
                                "WHERE id = ?", (r["id"],))
                            conn.commit()
                        low_src = rec_mod.RECORDINGS_ROOT / (r["file_path"][:-4]
                                                     + "/low.mp4")
                        if r["oss_low_key"]:
                            low_src.unlink(missing_ok=True)
                        try:
                            src.parent.with_name(
                                src.name[:-4]).rmdir()   # 空目录才成功
                        except OSError:
                            pass
                    # 手动切片释放（v2.1 零留存：原画质与低清按各自 keep，
                    # 服务器配置 0 = 上云即释；未上云的 low 不删，留给清扫
                    # 轮三级 glob 兜底）。
                    # v1.5.1 教训：cutoff 换算必须 * 3600（单位成秒会让
                    # 首轮扫描把全量存量当过期误删）
                    cutoff_orig = time.time() - keep_clips_h * 3600
                    cutoff_low = time.time() - clips_low_keep_h * 3600
                    for r in conn.execute(
                            "SELECT id, file_path, oss_low_key "
                            "FROM manual_clips "
                            "WHERE status='done' AND oss_key != '' "
                            "AND file_path LIKE '%.mp4'").fetchall():
                        f = rec_mod.RECORDINGS_ROOT / r["file_path"]
                        try:
                            if f.exists() and f.stat().st_mtime <= cutoff_orig:
                                f.unlink(missing_ok=True)
                        except OSError:
                            pass
                        if not f.exists() and r["oss_low_key"]:
                            low = rec_mod.RECORDINGS_ROOT / (r["file_path"][:-4]
                                                     + "/low.mp4")
                            try:
                                if low.exists() and \
                                        low.stat().st_mtime <= cutoff_low:
                                    low.unlink(missing_ok=True)
                                    try:
                                        low.parent.rmdir()   # 空目录才成功
                                    except OSError:
                                        pass
                            except OSError:
                                pass
                finally:
                    conn.close()
        except Exception as e:
            # 单轮扫描的任何异常都不杀线程（下轮重试）；打印而非静默，
            # 避免 schema/路径类问题被长期吞掉难排查
            print(f"[pipeline] _scan_pass 异常（本轮跳过）: {e!r}")


def _low_quality_worker() -> None:
    """转码 + 上云后台线程（web lifespan 内 daemon）：周期性跑 _scan_pass()，
    间隔 oss.scan_interval_sec（默认 10 分钟）；异常兜底不杀线程。

    v1.4：启动先睡满一个周期再扫。旧版「启动即扫」在转码持续失败且失败
    会拖死进程（如 cgroup OOM）时，与 systemd RestartSec=10 叠加成 13 秒
    一轮的崩溃紧循环（2026-10-08 事故，13 小时重启 3000+ 次）；先睡再扫
    后，重启风暴最快也只能 10 分钟一轮，配合 .part 失败墓碑（2 小时退
    避）与 MemoryMax 提升，三道防线齐备。正常路径代价仅是服务重启后的
    首轮转码/上云延迟一个扫描周期。"""
    interval = int(cfg.get("oss", {}).get("scan_interval_sec", 600))
    while True:
        time.sleep(interval)
        try:
            _scan_pass()
        except Exception:
            pass


def start() -> None:
    """启动后台管线线程（replay-web lifespan 调用，daemon 随进程退出）。"""
    import threading
    threading.Thread(target=_low_quality_worker, daemon=True,
                     name="low-quality").start()
