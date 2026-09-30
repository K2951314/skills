"""加密层安全测试：篡改、错口令、格式拒绝、来源优先级。"""

from __future__ import annotations

import io
import os
import zipfile

import pytest

from migrate_engine import EXIT_INTEGRITY, EXIT_PASSPHRASE
from migrate_engine.crypto import (
    MAGIC,
    CryptoError,
    decrypt_blob,
    encrypt_blob,
)


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_roundtrip():
    plain = _zip_bytes({"a.txt": b"hello"})
    blob = encrypt_blob(plain, "correct-horse-battery")
    assert blob.startswith(MAGIC)
    assert decrypt_blob(blob, "correct-horse-battery") == plain


def test_wrong_passphrase_fails_before_decrypt():
    blob = encrypt_blob(_zip_bytes({"a": b"x"}), "right-passphrase-1234")
    with pytest.raises(CryptoError) as exc:
        decrypt_blob(blob, "wrong-passphrase-1234")
    assert exc.value.exit_code == EXIT_PASSPHRASE


def test_tampered_ciphertext_detected():
    blob = bytearray(encrypt_blob(_zip_bytes({"a": b"x"}), "right-passphrase-1234"))
    blob[-1] ^= 0xFF
    with pytest.raises(CryptoError) as exc:
        decrypt_blob(bytes(blob), "right-passphrase-1234")
    assert exc.value.exit_code == EXIT_PASSPHRASE


def test_tampered_header_detected():
    blob = bytearray(encrypt_blob(_zip_bytes({"a": b"x"}), "right-passphrase-1234"))
    blob[2] ^= 0xFF  # salt 字节
    with pytest.raises(CryptoError):
        decrypt_blob(bytes(blob), "right-passphrase-1234")


def test_foreign_format_rejected():
    with pytest.raises(CryptoError):
        decrypt_blob(b"Salted__" + os.urandom(64), "whatever-passphrase")


def test_empty_cipher_rejected():
    with pytest.raises(CryptoError) as exc:
        decrypt_blob(MAGIC + os.urandom(16 + 32), "whatever-passphrase")
    assert exc.value.exit_code == EXIT_INTEGRITY


def test_unique_salt_per_export():
    a = encrypt_blob(b"same", "same-passphrase-1234")
    b = encrypt_blob(b"same", "same-passphrase-1234")
    assert a != b  # 随机 salt → 不同密文，无跨包密钥流复用


def test_empty_passphrase_rejected():
    with pytest.raises(CryptoError):
        encrypt_blob(b"x", "")
