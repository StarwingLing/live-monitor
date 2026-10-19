# -*- coding: utf-8 -*-
"""录制器：房间级 ffmpeg 分段录制（整场录制 / 直播截取 / 手动切片共用基建）。

整场录制（replay 站手动触发或 auto_full 自动对账，qn=full_qn 默认 10000
原画——v2.4.0 起，见 config.py 注释）：
全部分段保留，停止后 concat + remux 成单文件 mp4（-c copy 不转码，
+faststart 让 moov 前置，浏览器可拖动播放）。v2.1 起 2 小时自动分段轮换
+ 桥接片；v2.2 高能时刻移除，qn=80 滚动缓冲流随之退役（直播截取与打点
切片均作用在整场分段目录上，不受影响）。

目录布局（data/recordings/<room_id>/，data/ 不随部署同步）：
- full_<rec_id>/seg_*.ts   整场分段（录制期间持续追加；finalize 后删除）
- full_<rec_id>.mp4        整场成片（file_path 字段存相对本根目录的路径）
- bridge_<旧id>/seg_*.ts   v2.1 轮换桥接过渡素材（每日维护清扫兜底）
- manual/clip_<id>.mp4     手动切片成片（v0.11）

进程与生命周期：Recorder 由 RoomClient 持有（每个 enabled 房间一个实例，
惰性起流）。watcher 协程守护 ffmpeg：进程意外退出时查直播状态，在播则
重取拉流地址续段（指数退避封顶 60s——B站直链 token 时效短，重启一律
重新取地址）；未播则等待复播（与场次下播宽限同参数），由 RoomClient
确认下播时统一 finalize，watcher 自带超时保险兜底。短暂断播不拆录制，
最终汇入同一个成片。

web 进程不直接调用本模块：整场启停请求由 web 写 recordings 表
（status=requested / stop_requested），RoomManager 的 2s 扫描循环捡起后
调用 start_full / request_stop_full（见 manager._scan_recordings）。
"""
import asyncio
import datetime as dt
import json
import shutil
import time
from pathlib import Path

import aiohttp

from ..config import DATA_DIR, get_config
from ..db import now_iso

# 录制产物根目录（整场成片 / 高能成片 / 缓冲分段都在其下）
RECORDINGS_ROOT = DATA_DIR / "recordings"

# ---- B站拉流地址接口（三层兜底，均走登录 cookie 会话防风控）----
# ① 房间播放信息 v2（web 端现行接口，返回 playurl_info 结构）
PLAYINFO_API = "https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo"
# ② 老版 playUrl（返回 durl 直链列表）
PLAYURL_LEGACY_API = "https://api.live.bilibili.com/room/v1/Room/playUrl"
# ③ 房间聚合信息（响应自带 playurl_info，结构同①）
ROOM_INDEX_API = "https://api.live.bilibili.com/xlive/web-room/v1/index/getInfoByRoom"


def _rec_cfg() -> dict:
    """录制配置（进程内启动读一次，这里每次取缓存单例）。"""
    return get_config()["recording"]


def _ffmpeg_bin() -> str:
    """ffmpeg 可执行文件（config 可指向本地 tools/ffmpeg.exe）。"""
    return str(_rec_cfg().get("ffmpeg_path", "ffmpeg"))


def _ffprobe_bin() -> str:
    """ffprobe 路径：与 ffmpeg 同目录同名替换（ffmpeg_path 走 PATH 时
    ffprobe 也走 PATH；Windows 下自动补 .exe 后缀）。"""
    p = Path(_ffmpeg_bin())
    name = "ffprobe" + (".exe" if p.suffix.lower() == ".exe" else "")
    return str(p.with_name(name))


def _extract_playurl(playurl_info: dict | None) -> tuple[str, int] | None:
    """从 playurl_info 结构里挑最佳直链，返回 (url, current_qn)。

    结构（宽松解析，字段缺失即返回 None 让调用方降级下一层）：
    playurl_info.playurl.stream[] → 每项 protocol_name（flv / hls_fmp4 /
    hls / http_hls / fmp4）→ format[] → codec[]（codec_name avc/hevc；
    url_info[] 每项 {host, base_url, extra}，三者字符串直接拼接即完整 URL）。
    优先 flv（单条流，ffmpeg 直接拉），其次 hls 系（含 http_hls——
    v2.3.1 起白名单，延迟直播只发 http_hls，见 fetch_playurl）。
    """
    try:
        streams = (((playurl_info or {}).get("playurl") or {}).get("stream")) or []
        candidates: list[tuple[int, str, int]] = []  # (优先级, url, qn)
        for st in streams:
            proto = (st.get("protocol_name") or "").lower()
            if proto == "flv":
                prio = 0
            elif proto in ("hls_fmp4", "hls", "http_hls"):
                prio = 1
            elif proto == "fmp4":
                prio = 2
            else:
                continue
            for fmt in st.get("format") or []:
                for codec in fmt.get("codec") or []:
                    for ui in codec.get("url_info") or []:
                        url = f"{ui.get('host', '')}{ui.get('base_url', '')}{ui.get('extra', '')}"
                        if url.startswith("http"):
                            qn = codec.get("current_qn") or 0
                            candidates.append((prio, url, int(qn)))
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1], candidates[0][2]
    except Exception:
        return None


async def fetch_playurl(session: aiohttp.ClientSession, room_id: int,
                        qn: int) -> tuple[str, int]:
    """获取直播间拉流直链（三层兜底），返回 (url, current_qn)。

    直链带鉴权 token、时效短（分钟级），因此每次启动/断流重启都重新
    获取，绝不缓存复用。三层全部失败抛 RuntimeError（调用方决定置
    failed 还是交给 watcher 重试）。

    第一层 getRoomPlayInfo 按 flv（protocol=0）→ hls（protocol=1）
    两遍参数请求：延迟直播（主播开 300s 延迟档）下 flv 参数不发放任何
    直链（code=0 但 playurl 为空），hls 参数仍发放 http_hls 流——
    2026-10-19 某主播 300s 延迟场 70 分钟 138 条 failed 实证。flv 优先
    （单条流、分段录制最简），拿不到再问 hls，两层都空才降级下一层。
    """
    errs: list[str] = []
    # ① getRoomPlayInfo（v2）
    for proto in (0, 1):
        try:
            async with session.get(
                    PLAYINFO_API,
                    params={"room_id": room_id, "protocol": proto,
                            "format": 2, "codec": 0, "qn": qn,
                            "platform": "web", "ptype": 16, "dolby": 5,
                            "panorama": 1},
                    timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json(content_type=None)
            if data.get("code") == 0:
                got = _extract_playurl(
                    (data.get("data") or {}).get("playurl_info"))
                if got:
                    return got
            errs.append(f"getRoomPlayInfo(protocol={proto}) "
                        f"code={data.get('code')}")
        except Exception as e:
            errs.append(f"getRoomPlayInfo(protocol={proto}) {e!r}")
    # ② 老版 playUrl
    try:
        async with session.get(
                PLAYURL_LEGACY_API,
                params={"cid": room_id, "qn": qn, "platform": "web"},
                timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json(content_type=None)
        if data.get("code") == 0:
            durl = ((data.get("data") or {}).get("durl")) or []
            # 多 durl 为切片（罕见），取第一片：录制场景下后续片会在
            # 断流重启时重新获取，损失可忽略
            if durl and durl[0].get("url"):
                cur_qn = int((data.get("data") or {}).get("current_qn") or 0)
                return durl[0]["url"], cur_qn
        errs.append(f"playUrl code={data.get('code')}")
    except Exception as e:
        errs.append(f"playUrl {e!r}")
    # ③ getInfoByRoom（响应自带 playurl_info）
    try:
        async with session.get(
                ROOM_INDEX_API, params={"room_id": room_id},
                timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json(content_type=None)
        if data.get("code") == 0:
            got = _extract_playurl((data.get("data") or {}).get("playurl_info"))
            if got:
                return got
        errs.append(f"getInfoByRoom code={data.get('code')}")
    except Exception as e:
        errs.append(f"getInfoByRoom {e!r}")
    # 每层结果全量入报错文案（旧实现只留最后一层，2026-10-19 延迟直播
    # 事故的 reason 只剩「getInfoByRoom code=0」，掩盖了 flv 同为空的
    # 关键线索；逐层累积后 reason 一列即可定位）
    raise RuntimeError(f"房间{room_id} 三层拉流接口均失败：{'；'.join(errs)}")


# ffmpeg 拉流请求头：B 站直播 CDN 校验 User-Agent（ffmpeg 默认 Lavf UA
# 直接 403，已实测）与 Referer；与 manager.HTTP_HEADERS 同款浏览器 UA
FFMPEG_HEADERS = ("User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 "
                  "Safari/537.36\r\n"
                  "Referer: https://live.bilibili.com/\r\n")


def _seg_name_to_dt(name: str):
    """分段文件名 seg_%Y%m%dT%H%M%S.ts → datetime；解析失败返回 None。"""
    stem = Path(name).stem  # seg_20261001T123456
    try:
        return dt.datetime.strptime(stem[len("seg_"):], "%Y%m%dT%H%M%S")
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# v0.10 外挂弹幕：墙钟 → 视频内偏移 的时间轴映射
# JSON 结构（落 recordings.timeline / he_clips.timeline 列）：
#   {"runs": [{"wall": "2026-10-04T21:39:00", "off": 0.0, "dur": 1245.6}, ...],
#    "approx": false}
# 每个 ffmpeg 连续运行段（run）一行：wall=该 run 首段分段名时间；off=该 run
# 首帧在成片内的秒偏移（前序 runs 的 dur 累加）；dur=（尾段名−首段名）+
# ffprobe 实测尾段时长。run 划分按相邻分段名差 > segment_sec×1.5（正常间隔
# segment_sec；watcher 断流重启退避 ≥5s 后必然落在新段名上，缺口即断流点）。
# 断流期收到的弹幕在 API 映射时钳到 run 边界。
# ---------------------------------------------------------------------------
def build_timeline(seg_files: list[Path], segment_sec: int = 10) -> dict:
    """由分段文件名序列构建时间轴映射（纯函数，可单测）。

    seg_files 需按文件名排序（_pick_segments / finalize 收尾的 sorted 产物）；
    返回 dict（调用方 json.dumps 后落库）；无有效分段返回空 runs。
    run 的尾段时长用 ffprobe 实测（每个 run 仅 1 次子进程调用，失败按
    segment_sec 兜底——误差上限一个分段，10s 级）。
    """
    runs: list[dict] = []
    off = 0.0
    run_start: dt.datetime | None = None   # 当前 run 首段名时间
    prev_wall: dt.datetime | None = None   # 上一段的段名时间
    last_file: Path | None = None          # 上一段的文件（run 尾段探测用）

    def _close_run() -> None:
        """收拢当前 run：算时长、追加一行、偏移累加。"""
        nonlocal off, run_start
        if run_start is None or prev_wall is None or last_file is None:
            return
        tail_dur = Recorder._ffprobe_duration(last_file)
        if tail_dur is None:
            tail_dur = float(segment_sec)
        dur = (prev_wall - run_start).total_seconds() + tail_dur
        runs.append({"wall": run_start.strftime("%Y-%m-%dT%H:%M:%S"),
                     "off": round(off, 3), "dur": round(dur, 3)})
        off += dur
        run_start = None

    for f in seg_files:
        wall = _seg_name_to_dt(f.name)
        if wall is None:
            continue
        if run_start is None:
            run_start = wall
        elif (wall - prev_wall).total_seconds() > segment_sec * 1.5:
            _close_run()          # 缺口：prev_wall/last_file 属上一个 run 尾
            run_start = wall
        prev_wall = wall
        last_file = f
    _close_run()
    return {"runs": runs, "approx": False}


async def finalize_recording_row(db, rec_id: int, room_id: int,
                                 status: str, reason: str = "") -> None:
    """收尾一条整场录制（文件处理 + DB 更新），与 Recorder 实例无关。

    两条调用路径：
    - Recorder.finalize_full（停流后调用，见下）；
    - RoomManager 启动恢复 / 房间已移除时的孤儿行收尾（无实例场景）。

    分段 concat+remux 成 mp4（+faststart）；成功删分段目录，失败保留
    分段并把状态改记 failed（运维可手工重试）。duration 用 ffprobe 实测，
    失败以起止时间差兜底。
    """
    room_dir = RECORDINGS_ROOT / str(room_id)
    seg_dir = room_dir / f"full_{rec_id}"
    files = sorted(seg_dir.glob("seg_*.ts")) if seg_dir.exists() else []
    file_path, size, duration, timeline_json = "", 0, 0, ""
    # v1.1.1：拼接（大文件可达分钟级）开始前先落 'finalizing'——前端据
    # 此显示「保存中」而非「录制中」（replay.html 的 saving 判定早含此
    # 状态，v1.1 实测 10.4GB 整场拼接 7.5min 期间页面一直显示录制中）。
    # 若进程在拼接中崩溃，重启恢复（_recover_recordings）对本状态同样
    # 收尾，状态集已含 'finalizing'，语义闭环。
    db.execute("UPDATE recordings SET status = 'finalizing' WHERE id = ?",
               (rec_id,))
    out_mp4 = room_dir / f"full_{rec_id}.mp4"
    if files:
        if await Recorder._concat_remux(files, out_mp4):
            file_path = str(out_mp4.relative_to(RECORDINGS_ROOT))
            size = out_mp4.stat().st_size
            duration = int(Recorder._ffprobe_duration(out_mp4) or 0)
            # v0.10：删分段前把段名墙钟序列固化为时间轴映射（外挂弹幕对齐
            # 的唯一信息源；失败不影响成片，timeline 留空走 API 近似降级）
            try:
                timeline = build_timeline(files)
                if timeline["runs"]:
                    timeline_json = json.dumps(timeline, ensure_ascii=False)
            except Exception as e:
                print(f"[recorder] 房间{room_id} rec #{rec_id} 时间轴构建失败: {e!r}")
            # v0.11：录制收尾（下播/停止/重启恢复）时，仍在进行的直播片段
            # 截取（status='live'）在此封口并立即切出——分段马上要删，留给
            # 扫描循环处理必然扑空。每条快切约 2×segment_sec + 拼接时长，
            # 收尾变慢属预期（正常路径由按钮停止触发，不经过这里）。
            for lc in db.query("SELECT id FROM manual_clips "
                               "WHERE recording_id = ? AND status = 'live'",
                               (rec_id,)):
                db.execute("UPDATE manual_clips SET end_wall = ? "
                           "WHERE id = ?", (now_iso(), lc["id"]))
                print(f"[recorder] rec #{rec_id} 收尾：封口直播截取 #{lc['id']}")
                await run_manual_clip(db, lc["id"])
            shutil.rmtree(seg_dir, ignore_errors=True)
        else:
            status = "failed"
            reason = reason or "concat/remux 失败（分段已保留）"
    elif out_mp4.exists() and out_mp4.stat().st_size > 0:
        # v1.1.1：无分段但成片已在盘上——上次拼接已完成而行状态未及落库
        # 的进程中断窗口（重启恢复/孤儿行收尾走到这里）。直接采用现成
        # 成片，避免误判"无可用分段"丢掉整场录像。
        file_path = str(out_mp4.relative_to(RECORDINGS_ROOT))
        size = out_mp4.stat().st_size
        duration = int(Recorder._ffprobe_duration(out_mp4) or 0)
        print(f"[recorder] 房间{room_id} rec #{rec_id} 采用已存在成片"
              f"（{size / 1e6:.1f}MB，上次拼接已完成）")
    elif status == "finished":
        status = "failed"
        reason = reason or "无可用分段"

    row = db.query("SELECT started_at FROM recordings WHERE id = ?", (rec_id,))
    started = row[0]["started_at"] if row else now_iso()
    if not duration:
        # ffprobe 失败时用起止时间差兜底
        try:
            duration = int((dt.datetime.now()
                            - dt.datetime.strptime(started, "%Y-%m-%dT%H:%M:%S")
                            ).total_seconds())
        except ValueError:
            duration = 0
    db.execute(
        "UPDATE recordings SET status = ?, reason = ?, ended_at = ?, "
        "file_path = ?, file_size = ?, duration_sec = ?, timeline = ? WHERE id = ?",
        (status, reason, now_iso(), file_path, size, duration, timeline_json, rec_id))
    print(f"[recorder] 房间{room_id} 整场收尾 rec #{rec_id}：{status}"
          f"（{size / 1e6:.1f}MB, {duration}s）")


# ---------------------------------------------------------------------------
# v0.11 手动切片：无损快切（-c copy）
# ---------------------------------------------------------------------------
def build_cut_cmd(src: Path, start_sec: float, dur_sec: float,
                  out_mp4: Path) -> list[str]:
    """构造切片命令行（纯函数，可单测）。

    -ss 放在 -i 前（输入端 seek，直接跳到关键帧，秒级完成）+ -c copy
    无转码；代价是起点对齐到 ≤start 的最近关键帧（直播流 GOP 通常
    1~2s，实际偏差 ±1~2 秒，前端打点栏已有同口径提示）。
    -t 从 seek 后的位置起算时长；-avoid_negative_ts make_zero 处理
    copy 模式下起点非零时间戳的包；+faststart 让浏览器可拖动播放
    （与整场成片收尾同款）。
    """
    return [
        _ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-ss", f"{max(0.0, start_sec):.3f}",
        "-i", str(src),
        "-t", f"{max(0.0, dur_sec):.3f}",
        "-c", "copy", "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart",
        str(out_mp4),
    ]


def pick_segments_by_wall(seg_dir: Path, t_start: dt.datetime,
                          t_end: dt.datetime) -> list[Path]:
    """按墙钟窗口从分段目录挑段（直播切片模式，纯函数可单测）。

    与 Recorder._pick_segments 同一边界语义（段名时间与窗口有交集即算，
    宁多含一点画面），但作用在 full_<id>/ 分段目录上（录制进行中分段
    仍在追加，窗口尾段可能尚未封闭——由调用方先等 2×segment_sec）。
    """
    seg_sec = int(_rec_cfg().get("segment_sec", 10))
    out: list[Path] = []
    if not seg_dir.exists():
        return out
    for f in sorted(seg_dir.glob("seg_*.ts")):
        ft = _seg_name_to_dt(f.name)
        if ft is None:
            continue
        if ft <= t_end and ft + dt.timedelta(seconds=seg_sec) > t_start:
            out.append(f)
    return out


async def _restore_recording_from_oss(db, rec_id: int) -> Path | None:
    """v2.1 零留存配套：打点切片的源片不在本地时，从 OSS 内网回灌到原路径
    （同区域内网免流量费，~10GB 分钟级；断点续传与校验同
    deploy/restore_recording_from_oss.py 工艺）。回灌件 mtime=now，零留存
    释放分支（原片+low 双键齐备即删）下一轮扫描自动清掉，不占长期空间。
    行未上云 / 客户端不可用 / 下载失败一律返回 None（调用方标 failed）。"""
    row = db.query("SELECT file_path, oss_key, oss_bucket "
                   "FROM recordings WHERE id = ?", (rec_id,))
    if not row or not row[0]["oss_key"] or not row[0]["file_path"]:
        return None
    r = row[0]
    dst = RECORDINGS_ROOT / r["file_path"]
    if dst.exists():
        return dst
    part = dst.with_name(dst.name + ".restore-part")
    try:
        from ..web.pipeline import _oss_client   # 懒加载，避免 capture↔web 循环 import
        import alibabacloud_oss_v2 as oss
        client = _oss_client(internal=True)
        if not client:
            return None
        bucket = r["oss_bucket"] or get_config()["oss"]["bucket"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        print(f"[clip] 源片不在本地，回灌 rec #{rec_id}（{bucket}，内网）...")
        dl = oss.Downloader(client)
        dl.download_file(
            oss.models.object_basic.GetObjectRequest(bucket=bucket,
                                                     key=r["oss_key"]),
            filepath=str(part), part_size=8 << 20, parallel_num=2,
            enable_checkpoint=True,
            checkpoint_dir=str(DATA_DIR / "oss_checkpoints"))
        part.replace(dst)
        return dst
    except Exception as e:
        print(f"[clip] 回灌源片 rec #{rec_id} 失败: {e!r}")
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
        return None


async def run_manual_clip(db, clip_id: int) -> None:
    """执行一条 pending 切片：快切 → 落库 done/failed（模块级函数，
    与 finalize_recording_row 同风格，供 manager 扫描循环调用）。

    两种模式（以 start_wall 是否非空区分）：
    - 成片偏移模式：源为已收尾 mp4，-ss/-t 输入端快切；
    - 直播切片模式：源为录制中的 full_<id>/ 分段，按墙钟窗口挑段
      concat+remux（与高能出片同工艺）。窗口尾段可能仍在写入，先等
      2×segment_sec 让它封闭。录制恰在创建与执行之间收尾（分段已被
      finalize 清理）时标 failed——成片几秒内出现在板块，可改为对
      成片偏移重新切片，属可接受边缘。
    源成片行/文件缺失直接 failed（不重试）；ffmpeg 失败同样标 failed
    （用户可重新发起）。
    """
    row = db.query("SELECT * FROM manual_clips WHERE id = ?", (clip_id,))
    if not row:
        return
    clip = row[0]
    db.execute("UPDATE manual_clips SET status = 'processing' WHERE id = ?",
               (clip_id,))

    def _fail(reason: str) -> None:
        db.execute("UPDATE manual_clips SET status = 'failed', reason = ? "
                   "WHERE id = ?", (reason, clip_id))
        print(f"[clip] 切片 #{clip_id} 失败：{reason}")

    if clip["start_wall"]:
        # ---- 直播切片模式：从进行中的分段按墙钟窗口挑段 ----
        t0 = _parse_wall(clip["start_wall"])
        t1 = _parse_wall(clip["end_wall"])
        if t0 is None or t1 is None or t0 >= t1:
            _fail("直播切片窗口时间无法解析")
            return
        await asyncio.sleep(int(_rec_cfg().get("segment_sec", 10)) * 2)
        seg_dir = RECORDINGS_ROOT / str(clip["room_id"]) / f"full_{clip['recording_id']}"
        picked = pick_segments_by_wall(seg_dir, t0, t1)
        if not picked:
            _fail("窗口内无可用分段（录制可能已收尾，请对成片重新切片）")
            return
        out_mp4 = (RECORDINGS_ROOT / str(clip["room_id"]) / "manual"
                   / f"clip_{clip_id}.mp4")
        out_mp4.parent.mkdir(parents=True, exist_ok=True)
        if not await Recorder._concat_remux(picked, out_mp4):
            _fail("分段拼接失败")
            return
        duration = int(Recorder._ffprobe_duration(out_mp4) or 0)
        db.execute(
            "UPDATE manual_clips SET status = 'done', file_path = ?, "
            "file_size = ?, duration_sec = ?, reason = '' WHERE id = ?",
            (str(out_mp4.relative_to(RECORDINGS_ROOT)),
             out_mp4.stat().st_size, duration, clip_id))
        print(f"[clip] 直播切片 #{clip_id} 完成（{len(picked)} 段，"
              f"{out_mp4.stat().st_size / 1e6:.1f}MB, {duration}s）")
        return

    # ---- 成片偏移模式 ----
    src_row = db.query("SELECT file_path, duration_sec FROM recordings "
                       "WHERE id = ?", (clip["recording_id"],))
    if not src_row or not src_row[0]["file_path"]:
        _fail("源成片已被删除")
        return
    src = RECORDINGS_ROOT / src_row[0]["file_path"]
    if not src.exists():
        # v2.1 零留存：源片大概率已上云即释——内网回灌后再切（回灌件由
        # 零留存释放分支自动清理）；未上云或回灌失败才真正判 failed
        src = await _restore_recording_from_oss(db, clip["recording_id"])
        if src is None:
            _fail("源成片文件缺失（本地无副本且未上云/回灌失败）")
            return
    out_mp4 = RECORDINGS_ROOT / str(clip["room_id"]) / "manual" / f"clip_{clip_id}.mp4"
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    cmd = build_cut_cmd(src, float(clip["start_sec"]),
                        float(clip["end_sec"]) - float(clip["start_sec"]), out_mp4)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE)
        # 快切是纯 IO（copy），给足超时；超时杀进程标 failed
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=300)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            _fail("切片超时（300s）")
            return
        if proc.returncode != 0 or not out_mp4.exists():
            _fail(f"ffmpeg 退出码 {proc.returncode}："
                  f"{(err or b'')[-300:].decode('utf-8', 'ignore')}")
            return
    except Exception as e:
        _fail(f"切片异常: {e!r}")
        return
    duration = int(Recorder._ffprobe_duration(out_mp4) or 0)
    db.execute(
        "UPDATE manual_clips SET status = 'done', file_path = ?, file_size = ?, "
        "duration_sec = ?, reason = '' WHERE id = ?",
        (str(out_mp4.relative_to(RECORDINGS_ROOT)), out_mp4.stat().st_size,
         duration, clip_id))
    print(f"[clip] 切片 #{clip_id} 完成（{out_mp4.stat().st_size / 1e6:.1f}MB, "
          f"{duration}s）")


def _parse_wall(s: str) -> dt.datetime | None:
    """ISO 墙钟串 → datetime（与库内写入格式一致）；失败 None。"""
    try:
        return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
    except (ValueError, TypeError):
        return None


class Recorder:
    """单房间录制器（由 RoomClient 持有；不直接持有 aiohttp 会话，
    通过 client.manager.http_session 复用登录 cookie 会话）。"""

    def __init__(self, client):
        # client: capture.manager.RoomClient（延迟引用避免循环 import）
        self.client = client
        self.room_id = client.room_id
        self.room_dir = RECORDINGS_ROOT / str(self.room_id)

        # ---- 当前流状态 ----
        self.proc: asyncio.subprocess.Process | None = None  # ffmpeg 进程
        self._log_f = None                 # ffmpeg stderr 日志文件句柄
        self.stream_qn: int = 0            # 当前流画质（0=无流）
        self.seg_dir: Path | None = None   # 当前流分段输出目录
        # 当前应有流的状态：None=不需要流；'full'=整场（v2.2 高能缓冲已移除）
        self._desired: str | None = None

        # ---- 整场录制上下文 ----
        self.full_rec_id: int | None = None

        # ---- 后台协程 ----
        self._watcher: asyncio.Task | None = None
        # v2.1 分段轮换：2 小时到点轮换检查协程 + 当前段起始墙钟
        self._split_task: asyncio.Task | None = None
        self._full_started_wall: dt.datetime | None = None
        # 未开播计时（watcher 的宽限超时保险用）
        self._not_live_since: float | None = None

    # ------------------------------------------------------------------
    # 基础属性
    # ------------------------------------------------------------------
    @property
    def streaming(self) -> bool:
        """ffmpeg 进程是否仍在运行。"""
        return self.proc is not None and self.proc.returncode is None

    def _db(self):
        return self.client.manager.db

    def _session(self) -> aiohttp.ClientSession:
        return self.client.manager.http_session

    # ------------------------------------------------------------------
    # 流管理（起 / 停 / 切换）
    # ------------------------------------------------------------------
    async def ensure_stream(self, qn: int, seg_dir: Path) -> None:
        """确保有一路画质为 qn、输出到 seg_dir 的流在跑。

        - 已有同参数流：直接返回；
        - 无流或参数不同：停旧起新。
        起流失败抛 RuntimeError（desired 已设置、watcher 已启动，由
        调用方决定是否回滚状态）。
        """
        if (self.streaming and self.stream_qn == qn and self.seg_dir == seg_dir):
            return
        await self._terminate_proc()
        self.stream_qn = qn
        self.seg_dir = seg_dir
        await self._spawn()
        if self._watcher is None or self._watcher.done():
            self._watcher = asyncio.create_task(
                self._watcher_loop(), name=f"rec-watcher-{self.room_id}")

    async def _spawn(self) -> None:
        """取拉流地址并启动 ffmpeg 分段录制（不转码）。"""
        cfg = _rec_cfg()
        url, cur_qn = await fetch_playurl(self._session(), self.room_id,
                                          self.stream_qn)
        self.seg_dir.mkdir(parents=True, exist_ok=True)
        seg_tpl = str(self.seg_dir / "seg_%Y%m%dT%H%M%S.ts")
        # -reconnect*：http 层断线自动重连（B站 flv 直链）；
        # -headers：浏览器 UA + Referer（CDN 鉴权，缺 UA 会 403）；
        # -strftime 1：分段文件名带时间戳，高能出片按文件名时间挑段；
        # -reset_timestamps 1：每段时间戳归零，concat demuxer 可直接拼接。
        cmd = [
            _ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-headers", FFMPEG_HEADERS,
            "-reconnect", "1", "-reconnect_streamed", "1",
            "-reconnect_delay_max", "10",
            "-i", url,
            "-c", "copy", "-sn", "-dn",
            "-f", "segment",
            "-segment_time", str(int(cfg.get("segment_sec", 10))),
            "-reset_timestamps", "1",
            "-segment_format", "mpegts",
            "-strftime", "1",
            seg_tpl,
        ]
        # ffmpeg stderr 落日志文件（排障关键：403/直链过期等只报在 stderr）
        self.room_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.room_dir / "ffmpeg.log"
        self._log_f = open(log_path, "a", encoding="utf-8")
        self.proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=self._log_f)
        print(f"[recorder] 房间{self.room_id} ffmpeg 已启动（qn={self.stream_qn}"
              f"/实际{cur_qn}，目录 {self.seg_dir.name}，pid={self.proc.pid}）")

    async def _terminate_proc(self) -> None:
        """停 ffmpeg（terminate → 5s 宽限 → kill），关闭日志句柄。"""
        self._not_live_since = None
        await self._stop_proc(self.proc, self._log_f)
        self.proc = None
        self._log_f = None

    @staticmethod
    async def _stop_proc(proc, log_f) -> None:
        """停指定 ffmpeg 进程并关其日志句柄（v2.1 从 _terminate_proc 抽出：
        轮换等场景需要停「非当前实例上下文」的进程）。"""
        if proc is not None:
            try:
                if proc.returncode is None:
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        proc.kill()
                        await proc.wait()
            except ProcessLookupError:
                pass  # 进程已退出
        if log_f is not None:
            try:
                log_f.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 对外：整场录制（RoomManager 扫描循环调用）
    # ------------------------------------------------------------------
    async def start_full(self, rec_id: int, qn: int | None = None) -> None:
        """开始整场录制（recordings 行由 web 侧插入，这里接管执行）。

        qn：本次录制画质。缺省回退 full_qn（手动按钮页路径）；v0.11 起
        调用方（_scan_recordings）传 recordings.qn 列——行插入时已按来源
        定好（web 手动=full_qn 10000 / capture 自动对账=auto_full_qn
        10000，均原画档）。

        幂等防线（第三道）：本 Recorder 已在录时拒绝新请求行（作废为
        failed）——绝不能二次进入：否则会覆盖 full_rec_id 上下文并
        重启流，把进行中的录制杀掉且旧行永远卡在 recording 状态。
        """
        if self.full_rec_id is not None:
            await finalize_recording_row(self._db(), rec_id, self.room_id,
                                         "failed", "该房间已有进行中的录制")
            print(f"[recorder] 房间{self.room_id} 拒绝重复录制请求 rec #{rec_id}")
            return
        use_qn = int(qn or _rec_cfg().get("full_qn", 200))
        self.full_rec_id = rec_id
        self._desired = "full"
        try:
            await self.ensure_stream(use_qn,
                                     self.room_dir / f"full_{rec_id}")
        except Exception as e:
            print(f"[recorder] 房间{self.room_id} 整场启动失败: {e}")
            await self.finalize_full("failed", str(e))
            return
        self._db().execute(
            "UPDATE recordings SET status = 'recording', started_at = ?, qn = ? "
            "WHERE id = ?",
            (now_iso(), use_qn, rec_id))
        # v2.1：记录段起始墙钟并启动分段轮换检查协程（full_split_hours=0
        # 时协程空转，等价关闭）
        self._full_started_wall = dt.datetime.now()
        if self._split_task is None or self._split_task.done():
            self._split_task = asyncio.create_task(
                self._split_loop(), name=f"rec-split-{self.room_id}")
        print(f"[recorder] 房间{self.room_id} 整场录制开始（rec #{rec_id}）")

    async def request_stop_full(self, rec_id: int | None = None) -> None:
        """手动停止整场录制并保存（web 下发 stop_requested）。

        rec_id：目标行。当前实例正在录的就是它 → 正常收尾；不是（如
        capture 重启后实例丢了上下文）→ 直接按孤儿行收尾文件。"""
        if self.full_rec_id is not None and rec_id in (None, self.full_rec_id):
            await self.finalize_full("finished")
        elif rec_id is not None:
            await finalize_recording_row(self._db(), rec_id, self.room_id,
                                         "finished")

    async def finalize_full(self, status: str, reason: str = "") -> None:
        """收尾整场：停流 → concat+remux 成 mp4 → 更新 recordings 行。

        status：finished（正常结束）/ failed（起流失败等）/ interrupted
        （capture 重启或房间移除时对孤儿行的收尾）。文件处理与 DB 更新
        在模块级 finalize_recording_row（启动恢复复用同一段逻辑）。
        """
        rec_id = self.full_rec_id
        if rec_id is None:
            return
        self.full_rec_id = None
        self._desired = None
        await self._terminate_proc()

        await finalize_recording_row(self._db(), rec_id, self.room_id,
                                     status, reason)

    # ------------------------------------------------------------------
    # v2.1：分段轮换 + 桥接片（用户 2026-10-12 定稿规则）
    # ------------------------------------------------------------------
    async def rotate_now(self, reason: str = "分段轮换") -> None:
        """对外入口（manager 扫描 rotate_requested / _split_loop 到点调用）。"""
        await self._rotate_full(reason)

    async def _rotate_full(self, reason: str) -> None:
        """段落轮换：先停旧段再起新段（秒级接缝由桥接片覆盖）。

        顺序刻意为「先停后起」而非重叠双 ffmpeg：capture cgroup 内存上限内
        两路 ffmpeg 必然 OOM（2026-10-12 实测单路已顶到 599.5M/600M）；
        秒级接缝的代价与一次直链 token 过期断流相当，且桥接片（断点前后
        各 bridge_min 分钟）完整覆盖接缝两侧，观看无损。

        新段起流失败：新行标 failed、上下文清空——auto_full 房间由对账
        循环 30s 内自动重开新段（损失 ≤1 分钟）；旧段照常收尾不受影响。"""
        rec_id = self.full_rec_id
        if rec_id is None:
            return
        db = self._db()
        row = db.query("SELECT qn, source FROM recordings WHERE id = ?",
                       (rec_id,))
        if not row:
            return
        use_qn = int(row[0]["qn"] or 0) or int(_rec_cfg().get("full_qn", 200))
        source, part_qn = row[0]["source"], row[0]["qn"]
        bridge_min = int(_rec_cfg().get("bridge_min", 10))
        seg_sec = int(_rec_cfg().get("segment_sec", 10))
        need = max(1, bridge_min * 60 // seg_sec)   # 桥接单侧分段数
        # 1. 旧段尾分段先复制到过渡目录（旧段收尾 rmtree 不能带走桥接素材）。
        #    ~60 段 × 40MB ≈ 2.4GB 同步拷贝会卡死事件循环（弹幕处理停摆
        #    分钟级），放线程池执行。
        old_dir = self.seg_dir
        segs = sorted(old_dir.glob("seg_*.ts")) if old_dir else []
        staging = self.room_dir / f"bridge_{rec_id}"
        if segs and bridge_min > 0:
            def _stage():
                staging.mkdir(parents=True, exist_ok=True)
                for f in segs[-need:]:
                    shutil.copy2(f, staging / f.name)
            await asyncio.to_thread(_stage)
            segs = segs[-need:] if staging.exists() else []
        elif bridge_min <= 0:
            segs = []   # 桥接关闭：不留过渡素材
        # 2. 停旧段（ffmpeg 终止会封好最后一个分段文件）
        await self._terminate_proc()
        # 3. 起新段：插新行（直接 recording，不经 requested——防重三道防线
        #    会拒绝同房第二行）+ 重取直链起新 ffmpeg（顺带规避 B 站 token 时效）
        cur = db.execute(
            "INSERT INTO recordings (room_id, status, qn, created_at, "
            "started_at, source) VALUES (?, 'recording', ?, ?, ?, ?)",
            (self.room_id, use_qn, now_iso(), now_iso(), source))
        new_id = cur.lastrowid
        self.full_rec_id = new_id
        self.seg_dir = self.room_dir / f"full_{new_id}"
        self._full_started_wall = dt.datetime.now()
        try:
            await self._spawn()
        except Exception as e:
            print(f"[recorder] 房间{self.room_id} 轮换起新段失败: {e}")
            db.execute(
                "UPDATE recordings SET status = 'failed', reason = ?, "
                "ended_at = ? WHERE id = ?",
                (f"轮换起新段失败: {e}", now_iso(), new_id))
            self.full_rec_id = None
            self._desired = None
        # 4. 旧段收尾（独立任务，v1.1.1 模式；拼接 ffmpeg 与录制 ffmpeg 并存，
        #    部署需将 capture MemoryMax 提至 800M）
        db.execute("UPDATE recordings SET status = 'finalizing' WHERE id = ?",
                   (rec_id,))
        asyncio.create_task(self._finalize_part(rec_id, "finished", reason))
        # 5. 桥接片：新段录满 bridge_min 后拼「旧尾 + 新头」
        if segs:
            asyncio.create_task(
                self._build_bridge(staging, rec_id, new_id, need, seg_sec,
                                   int(part_qn or 0)),
                name=f"rec-bridge-{rec_id}")
        print(f"[recorder] 房间{self.room_id} 录制轮换：段 #{rec_id} → #{new_id}"
              f"（{reason}）")

    async def _finalize_part(self, rec_id: int, status: str,
                             reason: str = "") -> None:
        """轮换旧段的后台收尾（异常不外抛——行留 finalizing 由重启恢复兜底）。"""
        try:
            await finalize_recording_row(self._db(), rec_id, self.room_id,
                                         status, reason)
        except Exception as e:
            print(f"[recorder] 房间{self.room_id} 段 #{rec_id} 后台收尾异常: {e!r}")

    async def _build_bridge(self, staging: Path, old_id: int, new_id: int,
                            need: int, seg_sec: int, qn: int) -> None:
        """桥接片：拼接「旧段尾 + 新段头」各 bridge_min 分钟为 20 分钟成片。

        行 source='bridge'、started_at=桥接窗口起点（旧尾首段墙钟）——归档
        列表按 started_at 排序时天然落在上下两段之间；标题/命名与常规段
        同构（full_<id>.mp4），上云管线无需区分。新段头不足 need 段（轮换后
        很快下播）则有多少拼多少，两侧都空才跳过。"""
        try:
            new_dir = self.room_dir / f"full_{new_id}"
            await asyncio.sleep(need * seg_sec + 2 * seg_sec)  # 等新头录满
            old_tail = sorted(staging.glob("seg_*.ts"))
            new_head = (sorted(new_dir.glob("seg_*.ts"))[:need]
                        if new_dir.is_dir() else [])
            if not old_tail or not new_head:
                print(f"[recorder] 桥接 {old_id}↔{new_id} 素材不足，跳过"
                      f"（旧尾 {len(old_tail)} / 新头 {len(new_head)}）")
                shutil.rmtree(staging, ignore_errors=True)
                return
            files = old_tail + new_head
            wall_start = _seg_name_to_dt(old_tail[0].name) or dt.datetime.now()
            cur = self._db().execute(
                "INSERT INTO recordings (room_id, status, qn, created_at, "
                "started_at, source) VALUES (?, 'finalizing', ?, ?, ?, 'bridge')",
                (self.room_id, qn, now_iso(),
                 wall_start.strftime("%Y-%m-%dT%H:%M:%S")))
            bridge_id = cur.lastrowid
            out_mp4 = self.room_dir / f"full_{bridge_id}.mp4"
            if not await Recorder._concat_remux(files, out_mp4):
                self._db().execute(
                    "UPDATE recordings SET status = 'failed', ended_at = ?, "
                    "reason = '桥接片拼接失败' WHERE id = ?",
                    (now_iso(), bridge_id))
                shutil.rmtree(staging, ignore_errors=True)
                return
            size = out_mp4.stat().st_size
            duration = int(Recorder._ffprobe_duration(out_mp4)
                           or len(files) * seg_sec)
            timeline_json = ""
            try:
                tl = build_timeline(files)
                if tl["runs"]:
                    timeline_json = json.dumps(tl, ensure_ascii=False)
            except Exception:
                pass
            self._db().execute(
                "UPDATE recordings SET status = 'finished', file_path = ?, "
                "file_size = ?, duration_sec = ?, ended_at = ?, timeline = ?, "
                "reason = ? WHERE id = ?",
                (f"{self.room_id}/full_{bridge_id}.mp4", size, duration,
                 now_iso(), timeline_json,
                 f"轮换桥接（段 #{old_id} ↔ #{new_id}）", bridge_id))
            shutil.rmtree(staging, ignore_errors=True)
            print(f"[recorder] 桥接片 rec #{bridge_id} 完成（{len(old_tail)}"
                  f"+{len(new_head)} 段，{size / 1e6:.0f}MB）")
        except Exception as e:
            print(f"[recorder] 桥接 {old_id}↔{new_id} 构建异常: {e!r}")

    async def _split_loop(self) -> None:
        """分段轮换检查协程：整场录制墙钟超过 full_split_hours 且仍在播 →
        轮换。0 = 关闭（协程空转）。下播收尾等路径由 _desired 判断自然跳过。"""
        try:
            while True:
                await asyncio.sleep(30)
                if self._desired != "full" or self.full_rec_id is None:
                    continue
                hours = float(_rec_cfg().get("full_split_hours", 2) or 0)
                if hours <= 0 or self._full_started_wall is None:
                    continue
                elapsed = (dt.datetime.now()
                           - self._full_started_wall).total_seconds()
                if (elapsed >= hours * 3600
                        and await self._check_live()):
                    await self._rotate_full(
                        f"录制满 {hours:g} 小时自动轮换")
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # 对外：生命周期（RoomClient 状态机调用）
    # ------------------------------------------------------------------
    async def on_confirmed_end(self) -> None:
        """确认下播（场次宽限期结束）：整场在录 → 收尾保存。"""
        if self.full_rec_id is not None:
            await self.finalize_full("finished")

    async def on_stream_gone(self) -> None:
        """检测到未开播（v0.11：录制与场次宽限解耦）——整场录制**立即**
        收尾保存，不等 10 分钟场次宽限。

        动机：主播下播后录制空转宽限期全程（10 分钟无意义拉流）；且
        "短时间下播又复播"由对账循环自动起新段——接受多段录像（用户
        2026-10-05 定稿）。防 API 单次抖动误切：3 秒后复核一次房间状态，
        已复播则放弃收尾（录制无缝继续）；仍不在播才收尾。
        复核用 _check_live（复用登录会话）；收尾语义与确认下播相同
        （finished，成片保留完整）。宽限期内复播场景：本场次继续记数据
        （不拆场），录制作为独立一段已保存，复播段由对账循环 30s 内
        接续（手动录制的房间不自动重录，与现状一致）。
        """
        if self.full_rec_id is None:
            return
        await asyncio.sleep(3)
        if self.full_rec_id is None:
            return   # 复核等待期间已被停止/收尾（手动停止等），无事可做
        if await self._check_live():
            print(f"[recorder] 房间{self.room_id} 下播复核：3s 内复播，"
                  f"录制继续（rec #{self.full_rec_id} 不拆段）")
            return
        await self.finalize_full("finished", "检测到下播，录制即时收尾")

    async def shutdown(self) -> None:
        """房间移除 / 服务停止：整场收尾为 interrupted（已录部分保存）。"""
        if self.full_rec_id is not None:
            await self.finalize_full("interrupted", "服务停止或房间移除")
        else:
            self._desired = None
            await self._terminate_proc()
        for t in (self._watcher, self._split_task):
            if t is not None:
                t.cancel()
        self._watcher = self._split_task = None

    async def _check_live(self) -> bool:
        """查一次房间是否在播（watcher / 轮换重启用）。"""
        from .manager import fetch_room_info  # 函数内 import 避免循环依赖
        info = await fetch_room_info(self._session(), self.room_id)
        return info is not None and info.get("live_status") == 1

    # ------------------------------------------------------------------
    # 后台协程
    # ------------------------------------------------------------------
    async def _watcher_loop(self) -> None:
        """守护循环：ffmpeg 意外退出后自动续录。

        - 在播 → 指数退避（5s→60s 封顶）重取地址重启（直链 token 时效短，
          一律重新获取，不假设旧地址可用）；
        - 未播 → 30s 一轮等复播（RoomClient 确认下播会调用
          on_confirmed_end 收尾并把 _desired 置 None，本循环随之退出）；
          保险：未播持续超过 2×宽限+120s 且仍是整场，自行收尾
          （防 RoomClient 异常路径漏调）。
        """
        backoff = 5.0
        try:
            while True:
                await asyncio.sleep(5)
                if self._desired is None:
                    return
                if self.proc is None:
                    # v2.1：轮换窗口（旧停新未起，秒级）proc 短暂为 None——
                    # _desired 仍在则等它而不是退出；真正从未启动的场景由
                    # start_full 失败路径置 _desired=None，下一轮由此退出
                    await asyncio.sleep(2)
                    continue
                if self.proc.returncode is None:
                    backoff = 5.0            # 流活着：重置退避
                    self._not_live_since = None
                    continue
                # ---- 流断了 ----
                if await self._check_live():
                    self._not_live_since = None
                    print(f"[recorder] 房间{self.room_id} 流中断（仍在播），"
                          f"{backoff:.0f}s 后重连续录")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60.0)
                    if self._desired is None:
                        return
                    try:
                        await self._spawn()
                    except Exception as e:
                        print(f"[recorder] 房间{self.room_id} 重连失败: {e}")
                else:
                    if self._not_live_since is None:
                        self._not_live_since = time.monotonic()
                    elif (time.monotonic() - self._not_live_since
                          > 2 * float(get_config()["capture"]["end_grace_sec"]) + 120):
                        if self.full_rec_id is not None:
                            print(f"[recorder] 房间{self.room_id} 未播超时保险收尾")
                            await self.finalize_full("interrupted", "流中断后长时间未复播")
                        else:
                            self._desired = None
                            await self._terminate_proc()
                    await asyncio.sleep(30)
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # ffmpeg 工具
    # ------------------------------------------------------------------
    @staticmethod
    async def _concat_remux(files: list[Path], out_mp4: Path) -> bool:
        """分段 concat + remux 成 mp4（-c copy 无转码，秒级完成）。

        +faststart：把 moov box 移到文件头，浏览器 <video> 才能边下边播、
        支持拖动（配合 web 端 StaticFiles 的 HTTP Range 支持）。
        staticmethod：启动恢复（无 Recorder 实例）也要走同一段逻辑。
        """
        if not files:
            return False
        list_file = out_mp4.with_suffix(".txt")
        with open(list_file, "w", encoding="utf-8") as f:
            for p in files:
                # concat demuxer 语法：单引号包路径；路径内单引号转义
                line = str(p).replace("'", "'\\''")
                f.write(f"file '{line}'\n")
        cmd = [
            _ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-c", "copy", "-movflags", "+faststart", str(out_mp4),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL)
            # 大文件 remux 是纯 IO，给足超时（2GB 上限约需几分钟）
            await asyncio.wait_for(proc.wait(), timeout=600)
            ok = proc.returncode == 0 and out_mp4.exists()
            if not ok:
                print(f"[recorder] concat/remux 失败（returncode="
                      f"{proc.returncode}，{len(files)} 段）")
            return ok
        except Exception as e:
            print(f"[recorder] concat/remux 异常: {e!r}")
            return False
        finally:
            try:
                list_file.unlink()
            except OSError:
                pass

    @staticmethod
    def _ffprobe_duration(mp4: Path) -> float | None:
        """ffprobe 读取成片时长（秒）；失败返回 None（调用方用时间差兜底）。"""
        import subprocess
        try:
            out = subprocess.run(
                [_ffprobe_bin(), "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(mp4)],
                capture_output=True, text=True, timeout=60)
            return float(out.stdout.strip())
        except Exception:
            return None
