# -*- coding: utf-8 -*-
"""网页 Cookie 登录 CLI：浏览器 Cookie 串 → biliup 完整凭据（v2.3.0）。

等价 biliup-rs 登录菜单「网页Cookie登录1」（**无需扫码、无需 TTY**）：
TV 登录 auth_code → 网页会话（SESSDATA+bili_jct）h5/qrcode/confirm 自动
确认（scanning_type=3）→ 轮询 poll 取完整 LoginInfo（含 token_info.
access_token，新视频投稿的 APP 接口必需）。实现本体在
`replay/web/bili_api.py::exchange_web_login`（管理页·账号管理粘贴网页
Cookie 时同一条链路自动兑换）；本脚本是其命令行入口，供服务器/自动化
场景在 SESSDATA 过期后用浏览器重新导出的 Cookie 串刷新登录。

用法（在 replay 项目文件夹内）：
    uv run python deploy/biliup_web_login.py <cookie串文件> [输出路径]
    （cookie 串文件内容即浏览器导出的整段 k=v; …；默认输出
    data/biliup-cookies.json）

安全：凭据仅写入本项目 data/（不入 git），不经过任何第三方。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from replay.web import bili_api                     # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    raw = Path(sys.argv[1]).read_text("utf-8")
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 \
        else ROOT / "data" / "biliup-cookies.json"
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        info = bili_api.exchange_web_login(raw)
    except bili_api.BiliApiError as e:
        print(f"[失败] {e}")
        sys.exit(1)
    dst.write_text(bili_api.json.dumps(info, ensure_ascii=False, indent=1),
                   "utf-8")
    token = info.get("token_info") or {}
    print(f"[成功] 已兑换完整登录（access_token "
          f"{'有' if token.get('access_token') else '无'}），写入 {dst}")
    if token.get("mid"):
        print(f"[成功] mid = {token['mid']}")


if __name__ == "__main__":
    main()
