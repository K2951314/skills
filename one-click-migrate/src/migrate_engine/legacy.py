"""legacy 只读导入：智能询价历史包（openssl AES-256-CBC + PBKDF2 200k + 平铺 tar.gz）。

这些包没有 manifest、没有逐文件哈希、没有 HMAC。兼容策略：
- 只读导入，过**同一套**成员预检与事务写入；
- 判型靠内容标记（sqdb.dump/etc-sq.env → server；.env/keys → workspace；
  两类都有保守判 server——误合并是泄露，漏合并只是少恢复几个文件）；
- 报告里明示「旧包无完整性认证」；
- 不强制新口令长度规则（旧口令可能更短）。
"""

from __future__ import annotations

import hashlib
import io
import re
import tarfile
from pathlib import Path

from . import EXIT_PASSPHRASE, EXIT_UNSUPPORTED
from .crypto import CryptoError
from .pack import MAX_FILE_BYTES, MAX_MEMBERS, PACKAGE_FORMAT
from .unpack import PackageError, check_member_name

#: openssl 旧包的文件头。
LEGACY_MAGIC = b"Salted__"

_SERVER_MARKERS = ("sqdb.dump", "etc-sq.env")
_LOCAL_MARKERS = (".env", "keys")


def is_legacy_blob(blob: bytes) -> bool:
    return blob.startswith(LEGACY_MAGIC)


def _openssl_decrypt(blob: bytes, passphrase: str) -> bytes:
    """openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000，口令走 stdin（首行）。"""
    from . import platform as plat

    payload = passphrase.encode("utf-8") + b"\n" + blob

    openssl = plat.find_executable("openssl")
    if openssl is not None:
        # PATH 上有 openssl：密文经临时文件给（stdin 已被口令占用），用完即删
        import os
        import tempfile

        fd, tmp_path = tempfile.mkstemp(suffix=".enc")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(blob)
            rc, out, err = plat.run_binary(
                [openssl, "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000",
                 "-pass", "stdin", "-in", tmp_path],
                timeout=120.0,
                input_bytes=passphrase.encode("utf-8") + b"\n",
            )
        finally:
            Path(tmp_path).unlink(missing_ok=True)
        if rc != 0:
            raise CryptoError(
                f"旧包解密失败（口令错误或包损坏）：{err.strip()[:200]}",
                exit_code=EXIT_PASSPHRASE,
            )
        return out

    # Windows PATH 上没有 openssl：走 Git Bash（它内置 openssl）。
    # 口令与密文都走 stdin——`-pass stdin` 只读第一行当口令，其余是密文。
    bash = plat.find_executable("bash")
    if bash is None:
        raise PackageError(
            "导入旧格式包需要 openssl。请安装 Git for Windows（其 bash 内置 openssl），"
            "或把 openssl 加进 PATH。",
            exit_code=EXIT_UNSUPPORTED,
        )
    script = "openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass stdin"
    rc, out, err = plat.run_binary([bash, "-lc", script], timeout=120.0, input_bytes=payload)
    if rc != 0:
        raise CryptoError(
            f"旧包解密失败（口令错误或包损坏）：{err.strip()[:200]}",
            exit_code=EXIT_PASSPHRASE,
        )
    return out


def _tar_members(plain: bytes) -> list[tarfile.TarInfo]:
    """只读枚举 tar 成员（不落盘、不提取）。"""
    try:
        with tarfile.open(fileobj=io.BytesIO(plain), mode="r:gz") as tf:
            return list(tf.getmembers())
    except tarfile.TarError as exc:
        raise PackageError(f"旧包解密成功但不是合法 tar.gz：{exc}") from exc


def _read_member(plain: bytes, name: str) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(plain), mode="r:gz") as tf:
        fh = tf.extractfile(name)
        return fh.read() if fh is not None else b""


def open_legacy_package(path: Path, passphrase: str) -> tuple[dict, dict[str, tuple[bytes, str]]]:
    """打开旧包，返回（合成 manifest, {name: (data, sha256)}）。"""
    blob = path.read_bytes()
    if not is_legacy_blob(blob):
        raise CryptoError("不是 openssl 旧格式包（缺 Salted__ 头）。", exit_code=EXIT_PASSPHRASE)
    plain = _openssl_decrypt(blob, passphrase)

    members = _tar_members(plain)
    if not members:
        raise PackageError("旧包是空的。")
    if len(members) > MAX_MEMBERS:
        raise PackageError(f"旧包成员 {len(members)} 个，超过上限 {MAX_MEMBERS}。")

    files: dict[str, tuple[bytes, str]] = {}
    items: list[dict] = []
    for info in members:
        if not info.isfile():
            continue
        problem = check_member_name(info.name)
        if problem:
            raise PackageError(f"旧包成员不安全：{problem}")
        if info.size > MAX_FILE_BYTES:
            raise PackageError(f"旧包成员超过单文件上限：{info.name}")
        data = _read_member(plain, info.name)
        digest = hashlib.sha256(data).hexdigest()
        files[info.name] = (data, digest)
        items.append({
            "id": re.sub(r"[^a-z0-9]+", "-", info.name.lower()).strip("-") or "item",
            "path": info.name,
            "bytes": len(data),
            "sha256": digest,
            "class": "recommended",
            "sensitive": True,
            "item_type": "sqlite" if info.name.endswith((".db", ".sqlite", ".sqlite3")) else "file",
        })
    if not files:
        raise PackageError("旧包里没有普通文件。")

    names = set(files)
    has_server = any(m in names for m in _SERVER_MARKERS)
    has_local = any(m in names for m in _LOCAL_MARKERS) or any(n.startswith("keys/") for n in names)
    # 两类标记同时出现保守判 server：误合并是泄露，漏合并只是少恢复几个文件
    kind = "server" if (has_server or (has_server and has_local)) else "workspace"

    manifest = {
        "format": PACKAGE_FORMAT,
        "kind": kind,
        "project": "legacy",
        "engine": "legacy-reader",
        "created_at": None,
        "items": items,
        "rebuild": [],
        "verify": [],
        "legacy": True,
    }
    return manifest, files
