# -*- coding: utf-8 -*-
"""Web 端认证工具（v0.5：管理员/游客双角色登录）。

设计要点：
- 口令哈希：stdlib PBKDF2-HMAC-SHA256（12 万迭代 + 16 字节随机盐 +
  hmac.compare_digest 恒时比较），不引入任何第三方依赖；
- 会话：HMAC-SHA256 签名 Cookie（值 = "过期时间戳|角色|用户名|签名"），
  签名密钥 data/web_secret.key 首次启动自动生成（32 字节随机）；
  分隔符选 "|"，用户名/游客码的字符集限制保证不含分隔符；
- 角色：admin（全站读写）/ guest（只读）。X-Admin-Token 旧通道保留，
  与 capture.admin_password 比对成功视为 admin（兼容历史测试脚本；
  口令未配置则该通道不生效，同时全站不强制登录=本机调试模式）。
- v1.3.1：监控码功能（v0.9 的 replay 角色/一房一码登录）已整体迁往
  独立 replay 项目——本模块删除 gen/verify/record_replay_code、
  replay 角色解析与昵称段；共享库 rooms.replay_code 列与签名密钥保留
  （replay 项目读写/SSO 桥接自解析，不依赖本模块）。
"""
import hashlib
import hmac
import os
import secrets
import string
import time

from ..config import DATA_DIR, get_config
from ..db import connect, now_iso

COOKIE_NAME = "bili_auth"          # 登录会话 Cookie 名
SESSION_TTL = 30 * 24 * 3600       # 会话有效期：30 天（管理员与游客一致）
SECRET_PATH = DATA_DIR / "web_secret.key"   # Cookie 签名密钥（不入库、不同步）
CRED_FILE = DATA_DIR / "web_credentials.txt"  # 初始凭据明文的唯一落点

_PW_ALPHABET = string.ascii_letters + string.digits   # 随机口令字符集（无歧义字符外的全部字母数字）

# 管理员用户名约束：字母/数字/下划线/中划线（不含 Cookie 分隔符 "|"，杜绝注入签名结构）
USERNAME_OK = set(string.ascii_letters + string.digits + "_-")


# ---------------------------------------------------------------------------
# 口令哈希（PBKDF2）
# ---------------------------------------------------------------------------
def hash_secret(plain: str) -> str:
    """明文 → "pbkdf2_sha256$<iter>$<salt_hex>$<hash_hex>" 存储串。"""
    salt = os.urandom(16)
    iters = 120_000
    digest = hashlib.pbkdf2_hmac("sha256", plain.encode(), salt, iters)
    return f"pbkdf2_sha256${iters}${salt.hex()}${digest.hex()}"


def verify_secret(plain: str, stored: str) -> bool:
    """校验明文是否与存储串匹配（恒时比较；存储串格式异常按不匹配处理）。"""
    try:
        alg, iters, salt_hex, hash_hex = stored.split("$")
        if alg != "pbkdf2_sha256":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", plain.encode(),
                                   bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(calc.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


def gen_password(length: int = 16) -> str:
    """生成指定长度的随机字母数字口令（secrets 密码学随机源）。"""
    return "".join(secrets.choice(_PW_ALPHABET) for _ in range(length))


# ---------------------------------------------------------------------------
# 签名 Cookie 会话
# ---------------------------------------------------------------------------
def _secret() -> bytes:
    """读取（首次调用时生成）Cookie 签名密钥。"""
    if not SECRET_PATH.exists():
        SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
        SECRET_PATH.write_bytes(os.urandom(32))
    return SECRET_PATH.read_bytes()


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()


def sign_session(role: str, username: str = "", ttl: int = SESSION_TTL) -> str:
    """签发会话 Cookie 值："过期时间戳|角色|用户名|签名"（v1.3.1 起恒 4 段，
    v0.9.2 的昵称第 5 段随监控码功能迁往 replay 项目一并移除）。"""
    payload = f"{int(time.time()) + ttl}|{role}|{username}"
    return f"{payload}|{_sign(payload)}"


def read_session(request) -> dict | None:
    """从请求解析会话；无效/过期/被篡改返回 None。

    返回 {role, username}（v1.3.1 起 replay 角色不再被本站认可——监控码
    登录迁往 replay 项目，其旧会话由 replay 侧自行解析桥接）。"""
    val = request.cookies.get(COOKIE_NAME, "")
    parts = val.split("|")
    if len(parts) != 4:
        return None
    exp, role, username = parts[0], parts[1], parts[2]
    payload = "|".join(parts[:-1])   # 前三段参与验签（最后一段是签名）
    # 先验签再看过期（都失败时无法区分原因，统一按无效处理）
    if not hmac.compare_digest(_sign(payload), parts[-1]):
        return None
    try:
        if int(exp) < time.time() or role not in ("admin", "guest"):
            return None
    except ValueError:
        return None
    return {"role": role, "username": username}


def get_session(request) -> dict | None:
    """当前完整会话（read_session 的直通版，middleware 用它取 room_id）。"""
    return read_session(request)


def get_role(request) -> str | None:
    """请求当前角色：'admin' / 'guest' / None（未登录）。

    会话 Cookie 优先；无会话时回退 X-Admin-Token 旧通道（与
    capture.admin_password 匹配视为 admin——兼容既有测试脚本与运维习惯；
    口令未配置=本机调试模式，该通道不生效）。
    """
    session = read_session(request)
    if session is not None:
        return session["role"]
    expect = get_config()["capture"]["admin_password"]
    if expect and request.headers.get("X-Admin-Token") == expect:
        return "admin"
    return None


# ---------------------------------------------------------------------------
# 初始凭据种子（幂等）
# ---------------------------------------------------------------------------
def seed_web_credentials() -> None:
    """首次启动时生成默认管理员与游客码（幂等：仅当库中缺失时生成）。

    - 默认管理员：用户名 admin，随机 16 位口令；
    - 游客码：随机 12 位；
    - 明文一次性追加写入 data/web_credentials.txt（数据库只存哈希；
      该文件属本地敏感数据，不随部署同步、不进 git）；
    - 后续管理员/游客码的变更走管理页接口，不再写此文件。
    """
    conn = connect()
    created: list[tuple[str, str]] = []   # (用途, 明文) —— 仅本次新生成的
    try:
        if conn.execute("SELECT count(*) FROM admin_users").fetchone()[0] == 0:
            pw = gen_password(16)
            conn.execute(
                "INSERT INTO admin_users (username, password_hash, created_at) VALUES (?, ?, ?)",
                ("admin", hash_secret(pw), now_iso()))
            created.append(("管理员 admin 的口令", pw))
        if conn.execute(
                "SELECT count(*) FROM app_secrets WHERE key = 'guest_code'").fetchone()[0] == 0:
            code = gen_password(12)
            conn.execute(
                "INSERT INTO app_secrets (key, value) VALUES ('guest_code', ?)",
                (hash_secret(code),))
            created.append(("游客码", code))
        conn.commit()
    finally:
        conn.close()
    if created:
        CRED_FILE.parent.mkdir(parents=True, exist_ok=True)
        with CRED_FILE.open("a", encoding="utf-8") as f:
            f.write(f"\n===== 生成于 {now_iso()} =====\n")
            for usage, plain in created:
                f.write(f"{usage}: {plain}\n")
        print(f"[web-auth] 已生成初始登录凭据，明文见 {CRED_FILE}"
              "（仅本次写入，请妥善保存）")
