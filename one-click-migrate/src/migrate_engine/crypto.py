"""自包含加密层：零第三方依赖，防「U 盘丢失/网盘泄露」，不防对抗性攻击。

设计来源：zk-ai 项目 scripts/migrate.py 已跑通的同构实现（PBKDF2 +
计数器模式密钥流 + 摘要校验），本引擎在此基础上补两件事：
  1. 解密前先验 HMAC（encrypt-then-MAC）：口令错/密文被改，在解密前即失败；
  2. 版本化 magic：与本引擎以外的格式（如 openssl 旧包）明确区分。

威胁模型（诚实声明）：口令强度是唯一屏障；PBKDF2 120k 轮抗离线暴力，
HMAC 抗篡改。它不替代本机磁盘加密，也不提供前向保密。
"""

from __future__ import annotations

import hashlib
import hmac
import os

from . import EXIT_INTEGRITY, EXIT_PASSPHRASE

#: magic + 格式版本。旧包（openssl 的 Salted__ 头、zk-ai 的 ZKAI-MIGRATE）不会匹配。
MAGIC = b"OCMIG1"
SALT_LEN = 16
MAC_LEN = 32
KDF_ROUNDS = 120_000


class CryptoError(Exception):
    def __init__(self, message: str, *, exit_code: int):
        super().__init__(message)
        self.exit_code = exit_code


def _derive_keys(passphrase: str, salt: bytes) -> tuple[bytes, bytes]:
    """PBKDF2-SHA256 → 64 字节，前 32 加密、后 32 认证。"""
    dk = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, KDF_ROUNDS, dklen=64)
    return dk[:32], dk[32:]


def _keystream(key: bytes, length: int) -> bytes:
    """计数器模式密钥流：sha256(key || counter_le64)。随机 salt 保证每次导出的
    密钥流都不同，不存在跨包复用。"""
    out = bytearray()
    counter = 0
    while len(out) < length:
        out.extend(hashlib.sha256(key + counter.to_bytes(8, "little")).digest())
        counter += 1
    return bytes(out[:length])


def encrypt_blob(plain: bytes, passphrase: str) -> bytes:
    """blob = MAGIC || salt(16) || mac(32) || cipher。"""
    if not passphrase:
        raise CryptoError("口令不能为空。", exit_code=EXIT_PASSPHRASE)
    salt = os.urandom(SALT_LEN)
    enc_key, mac_key = _derive_keys(passphrase, salt)
    mask = _keystream(enc_key, len(plain))
    cipher = bytes(a ^ b for a, b in zip(plain, mask, strict=True))
    mac = hmac.new(mac_key, MAGIC + salt + cipher, hashlib.sha256).digest()
    return MAGIC + salt + mac + cipher


def decrypt_blob(blob: bytes, passphrase: str) -> bytes:
    """先验 HMAC 再解密。任何不匹配都在落盘前失败。"""
    if not blob.startswith(MAGIC):
        raise CryptoError(
            "不是 one-click-migrate 的迁移包（文件头不匹配）。"
            "旧格式包请用 legacy 导入路径（见 references/package-format.md）。",
            exit_code=EXIT_PASSPHRASE,
        )
    salt = blob[len(MAGIC):len(MAGIC) + SALT_LEN]
    mac = blob[len(MAGIC) + SALT_LEN:len(MAGIC) + SALT_LEN + MAC_LEN]
    cipher = blob[len(MAGIC) + SALT_LEN + MAC_LEN:]
    if not cipher:
        raise CryptoError("迁移包是空的（没有密文）。", exit_code=EXIT_INTEGRITY)
    enc_key, mac_key = _derive_keys(passphrase, salt)
    expect = hmac.new(mac_key, MAGIC + salt + cipher, hashlib.sha256).digest()
    if not hmac.compare_digest(expect, mac):
        raise CryptoError(
            "口令错误，或迁移包被篡改（认证失败）。目标没有任何改动。",
            exit_code=EXIT_PASSPHRASE,
        )
    mask = _keystream(enc_key, len(cipher))
    return bytes(a ^ b for a, b in zip(cipher, mask, strict=True))
