# -*- coding: utf-8 -*-
"""认证与会话（replay 项目：管理员 / 切片员 / 游客三角色 + SSO 桥接）。

设计要点（签名 Cookie 方案与 bili-monitor/web/auth.py 同源，改动的只有
角色集合与桥接逻辑）：
- 口令哈希不在本项目发生：管理员账户复用 bili-monitor 的 admin_users 表
  （哈希校验函数自 bili 同款实现，管理员账号两端一体）；
- 会话：HMAC-SHA256 签名 Cookie，值 = "过期时间戳|角色|用户名|签名"
  （带昵称时为 5 段："过期|角色|用户名|昵称|签名"，昵称百分号编码）；
  签名密钥为本项目自有 data/web_secret.key（首次使用自动生成，与 bili
  侧密钥互相独立）；
- 角色与绑定：
  - admin   全局权限，用户名槽位存管理员用户名（注释署名=用户名，与
            bili 侧口径一致）；
  - clipper 切片员：绑定一个房间（用户名槽位存房间号），昵称即切片/
            注释的署名身份——即原「监控码会话」的角色化命名，能力
            集合与原 replay 会话完全一致；
  - guest   游客：绑定一个房间，只读（浏览归档/播放/查切片/看注释）。
- 监控码：仍是「一房一码」（v1.3.x 房间表分离后存 replay_rooms.replay_code，
  本站自有名单表，明文入库），在此是
  **所有身份的公共入场凭证 + 房间导航**：登录页三角色都必须填，按码
  进入对应主播的回放页；
- SSO 桥接：本项目同时能验签 bili-monitor 的会话 Cookie（读其密钥文件）——
  bili 的 admin 会话自动续登为本项目 admin（bili 总览页点「录制」跳转
  后无感保持管理员）；bili 的旧监控码会话（role=replay）自动映射为对应
  房间的 clipper（存量用户零重登迁移）。bili 的 guest 不映射（监控站
  游客 ≠ 回放站游客，回放侧游客凭监控码进入）。
"""
import hashlib
import hmac
import os
import re
import secrets
import string
import time
from urllib.parse import quote, unquote

from .config import DATA_DIR, BILI_SECRET_PATH, BILI_COOKIE_NAME
from .db import connect

COOKIE_NAME = "replay_auth"        # 本项目会话 Cookie 名（独立于 bili_auth）
SESSION_TTL = 30 * 24 * 3600       # 会话有效期：30 天（与 bili 侧一致）
SECRET_PATH = DATA_DIR / "web_secret.key"   # 本项目 Cookie 签名密钥（不入库）

# 切片员昵称长度上限（字符数；中文也算 1 个字符，与 bili 侧 NICK_MAX 同口径）
NICK_MAX = 16

_PW_ALPHABET = string.ascii_letters + string.digits   # 随机码字符集

# 会话合法角色集合（签名校验后二次校验，防伪造角色段）
_ROLES = ("admin", "clipper", "guest")


# ---------------------------------------------------------------------------
# 口令校验（PBKDF2，与 bili-monitor 完全同款——共享库 admin_users 的
# 管理员口令哈希由此校验；切片员密码已改明文存储，不走哈希）
# ---------------------------------------------------------------------------
def verify_secret(plain: str, stored: str) -> bool:
    """校验明文口令与 "pbkdf2_sha256$<iter>$<salt>$<hash>" 存储串是否匹配
    （恒时比较；存储串格式异常按不匹配处理）。"""
    try:
        alg, iters, salt_hex, hash_hex = stored.split("$")
        if alg != "pbkdf2_sha256":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", plain.encode(),
                                   bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(calc.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# 本项目会话（签名 Cookie）
# ---------------------------------------------------------------------------
def _secret() -> bytes:
    """读取（首次调用时生成）本项目 Cookie 签名密钥。"""
    if not SECRET_PATH.exists():
        SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
        SECRET_PATH.write_bytes(os.urandom(32))
    return SECRET_PATH.read_bytes()


def _sign_with(secret: bytes, payload: str) -> str:
    return hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()


def sign_session(role: str, username: str = "", ttl: int = SESSION_TTL,
                 nickname: str = "") -> str:
    """签发会话 Cookie 值："过期时间戳|角色|用户名|签名"（昵称非空插第 4 段）。

    房间绑定角色（clipper/guest）把房间号放用户名槽位（纯数字，不含
    分隔符 "|"，与 bili 侧 v0.9 同一技巧）；昵称百分号编码（safe=""）
    后为纯 ASCII 且不含分隔符，签名结构不受昵称内容影响。"""
    payload = f"{int(time.time()) + ttl}|{role}|{username}"
    if nickname:
        payload += "|" + quote(nickname, safe="")
    return f"{payload}|{_sign_with(_secret(), payload)}"


def _parse_signed(val: str, secret: bytes, roles: tuple[str, ...]) -> dict | None:
    """通用的签名 Cookie 解析（本项目会话与 bili 桥接共用一段逻辑）。

    验签 → 过期 → 角色白名单，任一失败返回 None。"""
    parts = val.split("|")
    if len(parts) not in (4, 5):
        return None
    exp, role, username = parts[0], parts[1], parts[2]
    nickname = unquote(parts[3]) if len(parts) == 5 else ""
    payload = "|".join(parts[:-1])   # 最后一段是签名，前三/四段参与验签
    if not hmac.compare_digest(_sign_with(secret, payload), parts[-1]):
        return None
    try:
        if int(exp) < time.time() or role not in roles:
            return None
    except ValueError:
        return None
    return {"role": role, "username": username, "nickname": nickname}


def read_session(request) -> dict | None:
    """解析本项目会话；无效/过期/被篡改返回 None。

    返回 {role, username}；房间绑定角色（clipper/guest）额外带
    room_id（int，绑定房间）与 nickname（clipper 的署名；guest 恒空串）。"""
    val = request.cookies.get(COOKIE_NAME, "")
    out = _parse_signed(val, _secret(), _ROLES)
    if out is None:
        return None
    if out["role"] in ("clipper", "guest"):
        try:
            out["room_id"] = int(out["username"])
        except ValueError:
            return None   # 签名虽有效但房间号非法：按无效处理
    return out


# ---------------------------------------------------------------------------
# SSO 桥接：验签 bili-monitor 的会话 Cookie（共享密钥文件，只读）
# ---------------------------------------------------------------------------
def _bili_secret() -> bytes | None:
    """读 bili-monitor 的 Cookie 签名密钥；文件不存在（未部署/未同步）
    返回 None——桥接静默关闭，回放站独立登录不受影响。"""
    if not BILI_SECRET_PATH.exists():
        return None
    try:
        return BILI_SECRET_PATH.read_bytes()
    except OSError:
        return None


def read_bili_identity(request) -> dict | None:
    """解析 bili-monitor 会话并映射为本项目身份（桥接核心）。

    映射规则：
    - bili admin          → 本项目 admin（username 原样携带，注释署名
                            与 bili 总览页一致）；
    - bili replay（旧监控码会话）→ 本项目 clipper（绑定原房间 + 原昵称，
                            存量切片员零重登迁移）；
    - bili guest / 无效   → None（不映射）。
    """
    secret = _bili_secret()
    if secret is None:
        return None
    val = request.cookies.get(BILI_COOKIE_NAME, "")
    if not val:
        return None
    out = _parse_signed(val, secret, ("admin", "guest", "replay"))
    if out is None:
        return None
    if out["role"] == "admin":
        return {"role": "admin", "username": out["username"]}
    if out["role"] == "replay":
        # 旧监控码会话：用户名槽位存房间号、昵称即署名
        try:
            room_id = int(out["username"])
        except ValueError:
            return None
        return {"role": "clipper", "room_id": room_id,
                "nickname": out["nickname"]}
    return None   # bili guest 不映射


def bridge_session_value(identity: dict) -> str:
    """把桥接身份签发为本项目会话 Cookie 值（middleware 自动续登用）。"""
    if identity["role"] == "admin":
        return sign_session("admin", identity.get("username", ""))
    return sign_session("clipper", str(identity["room_id"]),
                        nickname=identity.get("nickname", ""))


# ---------------------------------------------------------------------------
# 切片员共用密码（v0.1.1；v0.1.2 改明文存储以支持管理页回显）
# ---------------------------------------------------------------------------
# 存储口径与监控码（replay_rooms.replay_code，本站自有名单表）一致：
# **明文入库 + 恒时比对校验**。
# 理由：管理页需随时查看/复制分发（哈希不可逆、无法回显）；泄露影响面
# 有限——切片员登录还需对应房间的监控码，且管理页本身仅管理员可达。
# 生成/重置时明文同时追加写凭据文件备份。所有切片员共用一个密码；昵称
# 仍是署名身份（密码不参与署名）。
CLIPPER_PW_KEY = "clipper_password"


def gen_password(length: int = 12) -> str:
    """生成随机字母数字口令（secrets 密码学随机源，与 bili 游客码同款）。"""
    return "".join(secrets.choice(_PW_ALPHABET) for _ in range(length))


def set_clipper_password(plain: str) -> None:
    """写入/重置切片员共用密码（明文入库，upsert）。"""
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO app_secrets (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (CLIPPER_PW_KEY, plain))
        conn.commit()
    finally:
        conn.close()


def get_clipper_password() -> str | None:
    """读取切片员共用密码明文（管理页回显用；未播种返回 None）。"""
    conn = connect()
    try:
        row = conn.execute("SELECT value FROM app_secrets WHERE key = ?",
                           (CLIPPER_PW_KEY,)).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def verify_clipper_password(plain: str) -> bool:
    """校验切片员共用密码（恒时比较；未播种/空输入均不通过）。"""
    stored = get_clipper_password()
    if not stored or not plain:
        return False
    return hmac.compare_digest(plain, stored)


def seed_clipper_password() -> str | None:
    """启动时确保切片员共用密码为可用明文（幂等 + 旧格式迁移）。

    - 库中无记录 → 生成新明文；
    - 库中为旧版 PBKDF2 哈希格式（v0.1.1 遗留，哈希不可逆无法回显）→
      重置为新明文（旧密码失效，新明文写凭据文件）；
    - 已是明文 → 不动，返回 None。
    新生成/迁移时明文追加写凭据文件（与监控码同款备份落点）。"""
    stored = get_clipper_password()
    if stored and not stored.startswith("pbkdf2_sha256$"):
        return None
    pw = gen_password(12)
    set_clipper_password(pw)
    try:
        cred = DATA_DIR / "web_credentials.txt"
        cred.parent.mkdir(parents=True, exist_ok=True)
        with cred.open("a", encoding="utf-8") as f:
            f.write(f"切片员共用密码（replay 回放站，全员共用）: {pw}\n")
    except OSError:
        pass
    return pw


# ---------------------------------------------------------------------------
# 监控码（一房一码；所有身份共用的入场凭证 + 房间导航）
# ---------------------------------------------------------------------------
_CODE_RE = re.compile(r"^r(\d+)-([A-Za-z0-9]{10})$")


def gen_replay_code(room_id: int) -> str:
    """生成监控码："r<房间号>-<10位随机>"（与 bili 侧同格式）。

    明文房间号前缀让登录校验无需遍历（解析前缀直接定位房间单次比对）；
    码本身明文入库（管理页随时显示/复制，泄露影响面=该房间的回放入场，
    敏感度可接受）。"""
    return f"r{room_id}-" + "".join(secrets.choice(_PW_ALPHABET) for _ in range(10))


def resolve_code(code: str) -> int | None:
    """监控码 → 房间号；无效/码不符返回 None。

    新登录动线只有「码」没有房间号输入框（按码自动进入对应主播页面），
    故先解析前缀定位房间、再恒时比对全码。"""
    m = _CODE_RE.match((code or "").strip())
    if not m:
        return None
    room_id = int(m.group(1))
    conn = connect()
    try:
        row = conn.execute(
            "SELECT replay_code FROM replay_rooms WHERE room_id = ?",
            (room_id,)).fetchone()
    finally:
        conn.close()
    stored = row[0] if row else ""
    if stored and hmac.compare_digest((code or "").strip(), stored):
        return room_id
    return None


def record_replay_code(room_id: int, code: str) -> None:
    """重置监控码后明文追加进本侧凭据文件（沿用既有备份落点：
    data/web_credentials.txt，本地敏感数据不进 git；管理页随时可查，
    这里只是追溯备份）。文件不可写时静默跳过（不阻断管理操作）。"""
    try:
        cred = DATA_DIR / "web_credentials.txt"
        cred.parent.mkdir(parents=True, exist_ok=True)
        with cred.open("a", encoding="utf-8") as f:
            f.write(f"监控码（房间{room_id}，replay 回放站登录）: {code}\n")
    except OSError:
        pass
