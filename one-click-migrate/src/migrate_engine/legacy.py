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
import stat
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


def normalize_member_name(name: str) -> str:
    """把旧包的 tar 成员名归一化成引擎能接受的形态。

    `tar -C <dir> .` 打出的每个成员名都带 `./` 前缀（真实脚本
    pull_from_server.sh 就是这样打的），而 check_member_name 无条件拒绝 `.`
    段——于是格式合法的旧包被当成「包内成员不安全」拒掉，报错还说包损坏。

    只在前导位置逐段剥离 `.`，中段的 `.` / `..` 一律保留，交给成员预检拒绝。
    这个宽松化仅对旧包生效，新包路径不做任何让步。
    """
    normalized = name
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized or name


def _openssl_decrypt(blob: bytes, passphrase: str) -> bytes:
    """openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000。

    口令与密文都走临时文件（``-pass file:`` + ``-in``），不用 ``-pass stdin``：
    OpenSSL 3.x 改了 stdin 的口令读取行为，旧版「第一行口令、其余密文」的
    玩法在新版上报 ``Error reading password from BIO``。临时文件用完即删。
    """
    import os
    import tempfile

    from . import platform as plat

    fd_key, key_path = tempfile.mkstemp(suffix=".key")
    fd_blob, blob_path = tempfile.mkstemp(suffix=".enc")
    try:
        with os.fdopen(fd_key, "wb") as fh:
            fh.write(passphrase.encode("utf-8") + b"\n")
        with os.fdopen(fd_blob, "wb") as fh:
            fh.write(blob)

        openssl = plat.find_executable("openssl")
        if openssl is not None:
            argv = [openssl, "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000",
                    "-pass", f"file:{key_path}", "-in", blob_path]
            rc, out, err = plat.run_binary(argv, timeout=120.0)
        else:
            # Windows PATH 上没有 openssl：走 Git Bash（它内置 openssl）。
            # 用 find_git_bash 而非 find_executable("bash")：PATH 上的 bash 可能是
            # WSL 存根，没装发行版时一调就挂。Git for Windows 的 bash 才有 openssl。
            bash = plat.find_git_bash()
            if bash is None:
                raise PackageError(
                    "导入旧格式包需要 openssl。请安装 Git for Windows（其 bash 内置 openssl），"
                    "或把 openssl 加进 PATH。",
                    exit_code=EXIT_UNSUPPORTED,
                )
            # 临时文件路径是 Windows 风格；Git Bash 的 openssl 接受正斜杠。
            key_posix = key_path.replace("\\", "/")
            blob_posix = blob_path.replace("\\", "/")
            script = (f"openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 "
                      f"-pass file:{_shquote(key_posix)} -in {_shquote(blob_posix)}")
            rc, out, err = plat.run_binary([bash, "-lc", script], timeout=120.0)
    finally:
        Path(key_path).unlink(missing_ok=True)
        Path(blob_path).unlink(missing_ok=True)

    if rc != 0:
        raise CryptoError(
            f"旧包解密失败（口令错误或包损坏）：{err.strip()[:200]}",
            exit_code=EXIT_PASSPHRASE,
        )
    return out


def _shquote(s: str) -> str:
    """给 bash 的单引号转义。路径不含单引号时就是 '' 包裹。"""
    return "'" + s.replace("'", "'\"'\"'") + "'"


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
        name = normalize_member_name(info.name)
        problem = check_member_name(name)
        if problem:
            raise PackageError(f"旧包成员不安全：{problem}")
        if info.size > MAX_FILE_BYTES:
            raise PackageError(f"旧包成员超过单文件上限：{info.name}")
        data = _read_member(plain, info.name)
        digest = hashlib.sha256(data).hexdigest()
        files[name] = (data, digest)
        items.append({
            "id": re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "item",
            "path": name,
            "bytes": len(data),
            "sha256": digest,
            "class": "recommended",
            "sensitive": True,
            "item_type": "sqlite" if name.endswith((".db", ".sqlite", ".sqlite3")) else "file",
            # 旧包 tar 里带了 mode；取不到就留空，导入侧按 sensitive 给 0600
            "mode": oct(stat.S_IMODE(info.mode))[2:].zfill(3) if info.mode else "",
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
