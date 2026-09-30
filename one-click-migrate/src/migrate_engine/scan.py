"""扫描与解析：把 manifest 条目解析成实际文件清单，计算快照与哈希。

目录/glob 展开规则：
- dir：递归展开为普通文件；跳过符号链接并告警；不跟随出项目的链接。
- glob：以 root 为基准 fnmatch；跳过 .git、产物目录、node_modules、.venv 等重目录。
- file/sqlite：单文件。包内名用声明的相对路径（不经 resolve），
  符号链接按「搬运内容」处理时包内名仍保持声明路径。
- sqlite：只解析库文件本身；打包用 backup API 生成快照（pack.py），
  sidecar 不发货；导入时清 stale sidecar（unpack.py）。
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

from .manifest import Item

#: 展开 glob/dir 时跳过的重目录名。
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache",
              ".pytest_cache", ".ruff_cache", ".tox", "dist", "build", "out", "coverage"}


def rel_posix(path: Path, root: Path) -> str:
    """词法相对路径（正斜杠）。不做 resolve，避免符号链接把包内名带出项目外。"""
    rel = os.path.relpath(str(path), str(root))
    return Path(rel).as_posix()


def sha256_file(path: Path, *, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


@dataclass
class ResolvedItem:
    item: Item
    files: list[Path] = field(default_factory=list)     # 绝对路径（未 resolve）
    archive_names: dict[str, Path] = field(default_factory=dict)  # 包内名 -> 绝对路径
    missing: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass
class FileSnap:
    relpath: str
    abspath: Path
    bytes: int
    sha256: str


def _walk_files(base: Path, root: Path, pattern: str | None) -> tuple[list[Path], list[str]]:
    notes: list[str] = []
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for name in filenames:
            p = Path(dirpath) / name
            rel = rel_posix(p, root)
            if pattern is not None and not fnmatch.fnmatch(rel, pattern):
                continue
            if p.is_symlink():
                notes.append(f"跳过符号链接：{rel}")
                continue
            if not p.is_file():
                notes.append(f"跳过非普通文件：{rel}")
                continue
            files.append(p)
    return sorted(files), notes


def resolve_item(root: Path, item: Item) -> ResolvedItem:
    """把一个条目解析成实际要搬运的文件集合。"""
    root = root.resolve()
    resolved = ResolvedItem(item=item)

    if item.item_type in {"dir", "glob"}:
        base = (root / item.path).resolve() if item.item_type == "dir" else root
        if item.item_type == "dir" and not base.is_dir():
            resolved.missing = True
            return resolved
        files, notes = _walk_files(base, root, item.path if item.item_type == "glob" else None)
        resolved.notes.extend(notes)
        if not files:
            resolved.missing = True
            return resolved
        resolved.files = files
        for p in files:
            resolved.archive_names[rel_posix(p, root)] = p
        return resolved

    # file / sqlite：单文件
    target = root / item.path
    if not target.exists():
        resolved.missing = True
        return resolved
    if not target.is_file():
        resolved.notes.append(f"条目不是普通文件：{item.path}")
        resolved.missing = True
        return resolved
    if target.is_symlink():
        resolved.notes.append(f"条目是符号链接，搬运其内容：{item.path}")
    resolved.files = [target]
    resolved.archive_names[item.path.replace("\\", "/")] = target
    return resolved


def snapshot_files(resolved: ResolvedItem, root: Path) -> list[FileSnap]:
    """对已解析条目逐个计算 体积 + sha256。只读内容算哈希，不解释内容。"""
    root = root.resolve()
    snaps: list[FileSnap] = []
    for abspath in resolved.files:
        snaps.append(FileSnap(
            relpath=resolved.archive_names and next(
                name for name, p in resolved.archive_names.items() if p == abspath),
            abspath=abspath,
            bytes=abspath.stat().st_size,
            sha256=sha256_file(abspath),
        ))
    return snaps
