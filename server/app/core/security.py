"""访问口令、会话令牌与 API Key 的存储安全。

设计取舍：
- **访问口令**：单用户免账号（决策 D-01），口令是唯一准入。库中只存 **Argon2id**
  派生串（含各自的随机盐）。校验走 argon2 库自身的 verify（恒定时间）。
  **为什么换掉 PBKDF2**：访问令牌时代刻意没上 argon2/bcrypt，理由是「输入是
  256-bit 全随机值而非人选口令，无字典攻击面」（见旧版本注释）。换成口令登录后
  这个前提不成立 —— 人选口令熵低，PBKDF2 在 GPU 上每秒几十亿次，正面抗不住字典，
  故改用内存��（Argon2id 的 64 MiB 内存代价让 ASIC/GPU 爆破代价高一个量级）。
- **会话令牌**：登录成功不把口令留在客户端反复发送，而是换发一个 HMAC 签名的
  会话令牌（决策见 ADR-0009）。**签名密钥直接由库里的口令哈希派生**，于是：
  - 不需要会话表，也不需要过期清理任务；
  - 改口令 = 换密钥 = 所有既有会话立刻失效（这是要的行为）；
  - 服务重启不掉线（密钥来自 DB 而非进程内存）；
  - 令牌里只有过期时间戳 + 签名，不含任何可还原的口令材料。
- **API Key**：存服务端供加工调用（决策 D-04），用 Fernet 对称加密。界面必须明示
  "Key 将存储在你的服务器上"。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings

# 口令最短长度。人选口令熵低，长度下限比复杂度规则更有效也更友好：
# 强制「必须含大小写数字符号」只会把人推向 `Password1!` 这种可预测组合。
MIN_PASSWORD_LEN = 8

# 会话有效期：单用户自用，登录一次能用一个月，避免每次开 app 都重输口令。
SESSION_TTL_SECONDS = 30 * 24 * 3600

# Argon2id 参数：OWASP 推荐基线（19 MiB / 2 轮 / 1 线程）。
# 单核机器上每次校验约 40~60 ms，对登录场景完全够用，且显著抬高离线爆破成本。
_hasher = PasswordHasher(
    time_cost=2, memory_cost=19 * 1024, parallelism=1, hash_len=32, salt_len=16
)

_PASSWORD_MIN_MESSAGE = f"口令至少 {MIN_PASSWORD_LEN} 位"


def validate_password_strength(password: str) -> None:
    """口令太弱直接拒掉。

    只做长度下限，不做「必须含大小写/数字/符号」这类组合规则 —— 那类规则被证明会
    把用户推向可预测的 `Password1!`，实际安全性反而下降。真正的防线是 Argon2id
    的高成本 + 不限失败次数（单用户本机服务，加速率限制只会让人自己被锁）。
    """
    if len(password) < MIN_PASSWORD_LEN:
        raise ValueError(_PASSWORD_MIN_MESSAGE)
    if len(password) > 1024:
        # argon2 对超长输入不截断，会白烧内存；显式挡住异常输入。
        raise ValueError("口令过长（上限 1024 字符）")


# --------------------------------------------------------------------------
# 访问口令
# --------------------------------------------------------------------------
def hash_password(password: str) -> str:
    """返回可直接入库的 Argon2id 编码串（含随机盐）。"""
    validate_password_strength(password)
    return _hasher.hash(password)


def verify_password(password: str, encoded: str) -> bool:
    """校验口令。哈希串被改坏或格式不认识都算失败，不抛异常。"""
    if not password or not encoded:
        return False
    try:
        _hasher.verify(encoded, password)
        return True
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


# --------------------------------------------------------------------------
# 会话令牌（HMAC 签名，密钥由口令哈希派生 —— 见 ADR-0009）
# --------------------------------------------------------------------------
def issue_session_token(password_hash: str, ttl_seconds: int = SESSION_TTL_SECONDS) -> str:
    """签发会话令牌，形如 ``<exp_b64url>.<sig_b64url>``。

    密钥 = ``SHA-256("strayt-session-v1" || password_hash)``。密码哈希本身已是
    高熵（Argon2 输出 32 字节）且只存在于 DB，足够当 HMAC 密钥用，不需要额外的
    随机密钥落盘（那会引入「密钥丢了就全登出」的额外故障点）。
    """
    exp = int(time.time()) + ttl_seconds
    payload = _b64u(str(exp).encode())
    return f"{payload}.{_b64u(_sign(password_hash, payload))}"


def verify_session_token(token: str, password_hash: str, now: float | None = None) -> bool:
    """校验会话令牌：签名要对且未过期。"""
    if not token or not password_hash:
        return False
    payload, _, sig_b64 = token.partition(".")
    if not payload or not sig_b64:
        return False
    try:
        expected = _sign(password_hash, payload)
        got = _unb64u(sig_b64)
    except (ValueError, TypeError):
        return False
    if not hmac.compare_digest(got, expected):
        return False
    try:
        exp = int(_unb64u(payload).decode())
    except (ValueError, UnicodeDecodeError):
        return False
    return exp > (time.time() if now is None else now)


def _sign(password_hash: str, payload: str) -> bytes:
    key = hashlib.sha256(b"strayt-session-v1" + password_hash.encode("utf-8")).digest()
    return hmac.new(key, payload.encode("ascii"), hashlib.sha256).digest()


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64u(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


# --------------------------------------------------------------------------
# API Key 密文
# --------------------------------------------------------------------------
def _fernet() -> Fernet:
    key = get_settings().secret_key
    if not key:
        raise RuntimeError(
            "STRAYT_SECRET_KEY 未配置，无法加解密 API Key。"
            "生成方式：在 server 目录执行 "
            "uv run python -c 'from cryptography.fernet import Fernet;"
            " print(Fernet.generate_key().decode())' 后把输出填进 server/.env"
        )
    # 允许直接填任意口令：派生出合法 32 字节 key
    if len(key) != 44:
        key = base64.urlsafe_b64encode(hashlib.sha256(key.encode("utf-8")).digest()).decode()
    return Fernet(key.encode("utf-8"))


def encrypt_secret(plain: str) -> str:
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_secret(cipher: str) -> str:
    try:
        return _fernet().decrypt(cipher.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:  # 密文被改或换了密钥
        raise RuntimeError("API Key 密文无法解密（STRAYT_SECRET_KEY 与入库时不一致？）") from exc


def mask_secret(plain: str) -> str:
    """展示用掩码：只露首 4 尾 4，界面不回传完整 Key。"""
    p = plain.strip()
    if len(p) <= 10:
        return "*" * len(p)
    return f"{p[:4]}{'*' * max(4, len(p) - 8)}{p[-4:]}"


def new_secret(prefix: str = "stp_") -> str:
    """生成高熵随机串（预留：例如用于服务端生成一次性会话标识）。"""
    return prefix + secrets.token_urlsafe(32)
