"""扫描与解析：把 manifest 条目解析成实际文件清单，计算快照与哈希。

目录/glob 展开规则：
- dir：递归展开为普通文件；跳过符号链接并告警；不跟随出项目的链接。
- glob：以 root 为基准 fnmatch；跳过 .git、产物目录、node_modules、.venv 等重目录。
- file/sqlite：单文件。包内名用声明的相对路径（不经 resolve），
  符号链接按「搬运内容」处理时包内名仍保持声明路径。
- sqlite：只解析库文件本身；打包用 backup API 生成快照（pack.py），
  sidecar 不发货；导入时清 stale sidecar（unpack.py）。

铁律：**产物目录自身永不出现在任何包里**。它里面是上一轮的加密包与导入时
备份的明文 .env / 私钥——把自己打进去，包会越打越大，且把明文密钥又复制了
一份给任何拿到这个包的人。所以展开时必须跳过 manifest.artifacts_dir、
引擎默认目录 .migrate、*.enc、backup-*、journal。
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

from .manifest import DEFAULT_ARTIFACTS_DIR, Item

#: 展开 glob/dir 时跳过的重目录名。
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache",
              ".pytest_cache", ".ruff_cache", ".tox", "dist", "build", "out", "coverage"}

#: 永远跳过的目录名（产物相关，与项目无关）。
_ARTIFACT_DIR_NAMES = frozenset({DEFAULT_ARTIFACTS_DIR, "journal"})

#: 永远跳过的文件/目录名模式（加密包与导入备份）。
_ARTIFACT_PATTERNS = ("*.enc", "backup-*", "*.tar.gz.enc")

#: 展开时因重目录规则被跳过的顶层目录名（汇报给用户，不静默丢弃）。
skipped_top_dirs: list[str] = []


def artifact_skip_names(artifacts_dir: str | None) -> set[str]:
    """本次展开要跳过的目录名集合 = 硬编码重目录 + 产物目录相关。"""
    names = set(_ARTIFACT_DIR_NAMES)
    if artifacts_dir:
        rel = artifacts_dir.replace("\\", "/").strip().strip("/")
        names.add(rel.split("/")[-1])       # 取最后一段：按目录名匹配
    return names


def _is_artifact_path(rel_posix_path: str, artifacts_dir: str | None) -> bool:
    """相对路径是否属于产物目录或产物文件（包/备份/journal）。"""
    parts = rel_posix_path.split("/")
    if artifacts_dir:
        rel = artifacts_dir.replace("\\", "/").strip().strip("/")
        top = rel.split("/")[0]
        # 产物目录本身或其内部任何层级
        if parts and parts[0] == top:
            return True
    if parts and parts[0] in _ARTIFACT_DIR_NAMES:
        return True
    base = parts[-1] if parts else ""
    return any(fnmatch.fnmatch(base, pat) for pat in _ARTIFACT_PATTERNS)



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


def _walk_files(base: Path, root: Path, pattern: str | None,
                skip_names: set[str] | None = None) -> tuple[list[Path], list[str]]:
    notes: list[str] = []
    files: list[Path] = []
    skip_names = skip_names or _SKIP_DIRS
    dropped_top: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(base):
        kept: list[str] = []
        for d in dirnames:
            if d in skip_names:
                dropped_top.add(d)
                continue
            kept.append(d)
        dirnames[:] = kept
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
    if dropped_top:
        # 不静默丢弃：用户看不出包里少了什么，是最难查的一类换机问题
        notes.append("已跳过目录（依赖/缓存/构建产物/换机产物）："
                     + ", ".join(sorted(dropped_top)))
    return sorted(files), notes


def resolve_item(root: Path, item: Item, artifacts_dir: str | None = None) -> ResolvedItem:
    """把一个条目解析成实际要搬运的文件集合。

    artifacts_dir 来自项目 manifest：产物目录（.enc、backup-*、journal）
    永不出现在包里，否则上一轮的加密包会被整包塞进新包。
    """
    root = root.resolve()
    resolved = ResolvedItem(item=item)
    skip_names = _SKIP_DIRS | artifact_skip_names(artifacts_dir)

    if item.item_type in {"dir", "glob"}:
        base = (root / item.path).resolve() if item.item_type == "dir" else root
        if item.item_type == "dir" and not base.is_dir():
            resolved.missing = True
            return resolved
        files, notes = _walk_files(base, root, item.path if item.item_type == "glob" else None,
                                   skip_names=skip_names)
        # 目录名过滤拦不住「产物目录里的文件被 glob 命中」——glob 是按完整路径
        # 匹配的，还要按路径再筛一遍（真实踩过：_换机/journal/leak.md 进包）。
        kept = [p for p in files
                if not _is_artifact_path(rel_posix(p, root), artifacts_dir)]
        dropped = len(files) - len(kept)
        resolved.notes.extend(notes)
        if dropped:
            resolved.notes.append(f"已跳过 {dropped} 个换机产物文件（{item.path} 命中产物目录）")
        if not kept:
            resolved.missing = True
            return resolved
        resolved.files = kept
        for p in kept:
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
