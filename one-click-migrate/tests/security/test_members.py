"""成员安全测试：路径穿越、链接、清单不符，全部在落盘前拒绝。"""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import zipfile

import pytest

from migrate_engine.crypto import encrypt_blob
from migrate_engine.pack import PACKAGE_FORMAT
from migrate_engine.unpack import PackageError, check_member_name, open_package

PW = "integration-passphrase-123"


def _manifest_with(names: list[str]) -> dict:
    return {
        "format": PACKAGE_FORMAT,
        "kind": "workspace",
        "project": "p",
        "items": [{"id": f"i{n}", "path": name, "bytes": 1,
                   "sha256": hashlib.sha256(b"x").hexdigest(),
                   "class": "required", "sensitive": False, "item_type": "file"}
                  for n, name in enumerate(names)],
    }


def _evil_package(tmp_path, names: list[str], *, extra_bytes: dict | None = None,
                  tamper_sha: str | None = None, drop_declared: bool = False) -> "Path":
    """构造一个「恶意/损坏」包：manifest 列出 names，zip 里放 names 的内容。"""
    manifest = _manifest_with(names)
    if drop_declared:
        manifest["items"] = manifest["items"][:1]
    if tamper_sha:
        manifest["items"][0]["sha256"] = tamper_sha
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        for name in names:
            zf.writestr(name, b"x")
    for name, data in (extra_bytes or {}).items():
        pass  # extra members handled below
    blob = encrypt_blob(buf.getvalue(), PW)
    path = tmp_path / "evil.enc"
    path.write_bytes(blob)
    return path


@pytest.mark.parametrize("bad", [
    "../evil.txt",
    "/etc/passwd",
    "C:\\Windows\\system32\\x",
    "\\\\srv\\share\\x",
    "a/../b.txt",
    "a//b.txt",
    "a/./b.txt",
    "dir/",
])
def test_check_member_name_rejects(bad):
    assert check_member_name(bad) is not None


@pytest.mark.parametrize("good", [".env", "a/b/c.txt", "_换机/notes.md", "data/app.db-wal"])
def test_check_member_name_allows(good):
    assert check_member_name(good) is None


def test_absolute_path_member_refused(tmp_path):
    pkg = _evil_package(tmp_path, ["/etc/passwd"])
    with pytest.raises(PackageError) as exc:
        open_package(pkg, PW)
    assert "不安全" in str(exc.value)


def test_parent_dir_member_refused(tmp_path):
    pkg = _evil_package(tmp_path, ["../evil.txt"])
    with pytest.raises(PackageError):
        open_package(pkg, PW)


def test_backslash_member_refused(tmp_path):
    pkg = _evil_package(tmp_path, ["C:\\x"])
    with pytest.raises(PackageError):
        open_package(pkg, PW)


def test_undeclared_member_refused(tmp_path):
    # zip 里多一个 manifest 没写的文件
    manifest = _manifest_with(["ok.txt"])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("ok.txt", b"x")
        zf.writestr("sneaky.txt", b"x")
    pkg = tmp_path / "sneaky.enc"
    pkg.write_bytes(encrypt_blob(buf.getvalue(), PW))
    with pytest.raises(PackageError) as exc:
        open_package(pkg, PW)
    assert "sneaky.txt" in str(exc.value)


def test_hash_mismatch_refused(tmp_path):
    pkg = _evil_package(tmp_path, ["a.txt"], tamper_sha="0" * 64)
    with pytest.raises(PackageError) as exc:
        open_package(pkg, PW)
    assert "哈希" in str(exc.value)


def test_declared_but_absent_refused(tmp_path):
    pkg = _evil_package(tmp_path, ["a.txt", "b.txt"], drop_declared=True)
    with pytest.raises(PackageError) as exc:
        open_package(pkg, PW)
    assert "b.txt" in str(exc.value)


def test_symlink_member_refused(tmp_path):
    manifest = _manifest_with(["link"])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        info = zipfile.ZipInfo("link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, "/etc/passwd")
    pkg = tmp_path / "link.enc"
    pkg.write_bytes(encrypt_blob(buf.getvalue(), PW))
    with pytest.raises(PackageError) as exc:
        open_package(pkg, PW)
    assert "符号链接" in str(exc.value)


def test_wrong_kind_field_refused(tmp_path):
    manifest = _manifest_with(["a.txt"])
    manifest["kind"] = "bogus"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("a.txt", b"x")
    pkg = tmp_path / "kind.enc"
    pkg.write_bytes(encrypt_blob(buf.getvalue(), PW))
    with pytest.raises(PackageError) as exc:
        open_package(pkg, PW)
    assert "kind" in str(exc.value)
