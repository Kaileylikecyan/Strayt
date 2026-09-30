"""访问令牌与 API Key 的存储安全。

设计取舍：
- **访问令牌**：单用户免账号（决策 D-01），令牌是唯一准入。生成 256-bit 随机串，
  库中只存 PBKDF2-HMAC-SHA256 派生值 + 盐。校验用恒定时间比较。
  不引入 argon2/bcrypt 依赖 —— 输入是 256-bit 全随机值而非人选口令，无字典攻击面。
- **API Key**：存服务端供加工调用（决策 D-04），用 Fernet 对称加密。界面必须明示
  "Key 将存储在你的服务器上"。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings

_PBKDF2_ROUNDS = 240_000
_SALT_BYTES = 16


def _pepper() -> bytes:
    """取访问令牌 pepper。

    配置里显式给了就用配置值。**没给绝不能退化成空 pepper** —— 空 pepper 等于
    所有部署共用同一把「盐外」密钥，DB 一旦泄露就直接离线爆破。

    这里改为：首次缺失时随机生成并落盘到 ``data_dir/access_token_pepper``，
    之后稳定复用。语义比「每次启动随机」好（重启不会踢掉已配置的客户端），
    又不会静默降级成空 pepper。生产仍应显式配置以便备份恢复。
    """
    configured = get_settings().access_token_pepper.strip()
    if configured:
        return configured.encode("utf-8")

    cache = get_settings().data_dir / "access_token_pepper"
    if cache.exists():
        return cache.read_bytes().strip()
    cache.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_bytes(32)
    cache.write_bytes(value)
    cache.chmod(0o600)
    return value


# --------------------------------------------------------------------------
# 访问令牌
# --------------------------------------------------------------------------
def generate_access_token() -> str:
    """生成给人用的明文令牌（只在生成时出现一次，服务端不保存明文）。"""
    return "stt_" + secrets.token_urlsafe(32)


def hash_access_token(token: str) -> str:
    """返回可直接入库的编码串：``pbkdf2$<rounds>$<salt_b64>$<dk_b64>``"""
    salt = secrets.token_bytes(_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", token.encode("utf-8"), _pepper() + salt, _PBKDF2_ROUNDS)
    return (
        f"pbkdf2${_PBKDF2_ROUNDS}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"
    )


def verify_access_token(token: str, encoded: str) -> bool:
    try:
        scheme, rounds, salt_b64, dk_b64 = encoded.split("$")
        if scheme != "pbkdf2":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
        rounds_i = int(rounds)
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", token.encode("utf-8"), _pepper() + salt, rounds_i)
    return hmac.compare_digest(actual, expected)


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
