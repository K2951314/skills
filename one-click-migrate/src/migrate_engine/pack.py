"""打包：内存 zip + OCMIG1 加密，导出后回读校验。

铁律：
- 明文 zip 只在内存（io.BytesIO），不落临时文件。
- SQLite 走 backup API 一致性快照；-wal/-shm/-journal 不发货。
- 写完 .enc 立刻解密回读并重算每个文件 sha256，对不上就删包报错——
  不留「看起来成功」的坏包。
"""

from __future__ import annotations

import io
import json
import sqlite3
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import EXIT_ERROR, __version__
from .crypto import CryptoError, encrypt_blob
from .manifest import Manifest
from .scan import resolve_item, snapshot_files

KIND_WORKSPACE = "workspace"
KIND_SERVER = "server"

PACKAGE_FORMAT = 1
MAX_MEMBERS = 10000
MAX_FILE_BYTES = 1 << 30  # 1 GiB


@dataclass
class PackEntry:
    id: str
    relpath: str          # 包内名（正斜杠相对路径）
    abspath: Path
    data: bytes
    sha256: str
    bytes: int
    cls: str
    sensitive: bool
    item_type: str


@dataclass
class PackResult:
    path: Path
    kind: str
    project: str
    bytes: int
    entries: list[PackEntry] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


def _sqlite_snapshot_bytes(db_path: Path) -> bytes:
    """SQLite 在线备份 → 内存字节。崩溃一致，与正在写的进程互不干扰。"""
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(":memory:")
        try:
            src.backup(dst)
            return dst.serialize()
        finally:
            dst.close()
    finally:
        src.close()


def collect_entries(root: Path, manifest: Manifest, items) -> tuple[list[PackEntry], list[str]]:
    """解析条目 → 逐文件条目。必需项缺失直接抛错（导出前止损）。"""
    entries: list[PackEntry] = []
    missing: list[str] = []
    for item in items:
        resolved = resolve_item(root, item)
        if resolved.missing:
            if item.on_missing == "block":
                raise ExportError(
                    f"必需项 {item.id}（{item.path}）不存在，导出中止。\n"
                    "确认它真的被排除了、且这台机器上应该有；否则改 manifest 的 on_missing。"
                )
            missing.append(f"{item.id}（{item.path}）")
            continue
        snaps = snapshot_files(resolved, root)
        for snap in snaps:
            if item.item_type == "sqlite":
                data = _sqlite_snapshot_bytes(snap.abspath)
                digest = _sha256_bytes(data)
            else:
                data = snap.abspath.read_bytes()
                digest = snap.sha256
            entries.append(PackEntry(
                id=item.id, relpath=snap.relpath, abspath=snap.abspath,
                data=data, sha256=digest, bytes=len(data),
                cls=item.cls, sensitive=item.sensitive, item_type=item.item_type,
            ))
    return entries, missing


class ExportError(Exception):
    def __init__(self, message: str, *, exit_code: int = EXIT_ERROR):
        super().__init__(message)
        self.exit_code = exit_code


def _sha256_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def build_payload_zip(entries: list[PackEntry], *, kind: str, project: str,
                      rebuild: list[str], verify: list[str]) -> bytes:
    """manifest.json + payload 文件，全部在内存。"""
    manifest = {
        "format": PACKAGE_FORMAT,
        "kind": kind,
        "project": project,
        "engine": __version__,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "hostname_hint": _hostname_hint(),
        "items": [
            {
                "id": e.id,
                "path": e.relpath,
                "bytes": e.bytes,
                "sha256": e.sha256,
                "class": e.cls,
                "sensitive": e.sensitive,
                "item_type": e.item_type,
            }
            for e in entries
        ],
        "rebuild": rebuild,
        "verify": verify,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for e in entries:
            zf.writestr(e.relpath, e.data)
    return buf.getvalue()


def _hostname_hint() -> str:
    import socket

    try:
        return socket.gethostname()
    except OSError:
        return "unknown"


def _default_out(artifacts_dir: Path, project: str, kind: str, stamp: str) -> Path:
    return artifacts_dir / f"{project}-{kind}-{stamp}.enc"


def export_package(root: Path, manifest: Manifest, items, *, kind: str, passphrase: str,
                   stamp: str | None = None) -> PackResult:
    """打包并自校验。任何一步失败都不留坏包。"""
    root = root.resolve()
    artifacts_dir = root / manifest.artifacts_dir
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    out = _default_out(artifacts_dir, manifest.project, kind, stamp)

    entries, missing = collect_entries(root, manifest, items)
    if not entries:
        raise ExportError("没有任何可打包的文件——先跑 plan 确认清单。")

    zip_bytes = build_payload_zip(
        entries, kind=kind, project=manifest.project,
        rebuild=manifest.rebuild, verify=manifest.verify,
    )
    blob = encrypt_blob(zip_bytes, passphrase)
    out.write_bytes(blob)

    # 导出后回读：解密 → 解 zip → 逐文件重算 sha256 → 与 manifest 对账
    from .unpack import PackageError, open_package  # 延迟导入：unpack 不依赖本模块

    try:
        info, files = open_package(out, passphrase)
    except (CryptoError, PackageError) as exc:
        out.unlink(missing_ok=True)
        raise ExportError(f"导出后回读失败，已删除坏包：{exc}") from exc
    for record in info["items"]:
        data, digest = files[record["path"]]
        if digest != record["sha256"] or len(data) != record["bytes"]:
            out.unlink(missing_ok=True)
            raise ExportError(f"导出后回读不一致，已删除坏包：{record['path']}")

    return PackResult(path=out, kind=kind, project=manifest.project,
                      bytes=out.stat().st_size, entries=entries, missing=missing)
