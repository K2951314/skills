"""导入事务日志：崩溃可恢复，不留下半导入状态。

journal 是 JSONL，一行一次操作：
  {"op": "backup",  "dst": "...", "backup": "..."}
  {"op": "write",   "tmp": "...", "dst": "...", "sha256": "..."}
  {"op": "commit",  "dst": "...", "backup": "...", "sha256": "..."}
  {"op": "skip",    "dst": "..."}
  {"op": "done",    "ok": true}

recover() 规则：
  - write 了但没 commit → tmp 是半成品，删除；
  - commit 过但 dst 缺失或哈希不符 → 有备份则从备份拷回；
  - 无备份的 commit（目标原本不存在）→ dst 是导入产生的，删除它。
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path

from .scan import sha256_file


def journal_dir(artifacts_dir: Path) -> Path:
    d = artifacts_dir / "journal"
    d.mkdir(parents=True, exist_ok=True)
    return d


def journal_path(artifacts_dir: Path, stamp: str) -> Path:
    return journal_dir(artifacts_dir) / f"import-{stamp}.jsonl"


class Journal:
    def __init__(self, path: Path):
        self.path = path
        self._fh = path.open("a", encoding="utf-8")

    def log(self, **record) -> None:
        record["ts"] = datetime.now().astimezone().isoformat(timespec="seconds")
        self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def close(self) -> None:
        self._fh.close()


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass  # 平台不支持目录 fsync 时尽力而为


def atomic_commit(*, tmp: Path, dst: Path, data: bytes, sha256: str) -> None:
    """写临时文件 → fsync → 校验 → 原子替换 → fsync 目录。"""
    tmp.write_bytes(data)
    with tmp.open("rb+") as fh:
        fh.flush()
        os.fsync(fh.fileno())
    from hashlib import sha256 as _sha

    if _sha(data).hexdigest() != sha256:
        tmp.unlink(missing_ok=True)
        raise ValueError(f"写入内容与包内哈希不一致：{dst.name}")
    os.replace(tmp, dst)
    _fsync_dir(dst.parent)


def recover(artifacts_dir: Path, journal_file: Path) -> list[str]:
    """重放一个 journal，尽力把目标恢复到导入前状态。返回动作描述。"""
    actions: list[str] = []
    if not journal_file.is_file():
        return [f"journal 不存在：{journal_file}"]

    writes: dict[str, dict] = {}     # tmp -> record
    commits: dict[str, dict] = {}    # dst -> record
    order: list[str] = []
    for line in journal_file.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        op = rec.get("op")
        if op == "write":
            writes[rec["tmp"]] = rec
        elif op == "commit":
            commits[rec["dst"]] = rec
            order.append(rec["dst"])
        elif op == "skip":
            order.append(rec["dst"])

    # 1. 半成品临时文件：写了没提交，删
    for tmp, rec in writes.items():
        if rec.get("dst") not in commits:
            p = Path(tmp)
            if p.exists():
                p.unlink()
                actions.append(f"删除半成品 {p.name}")

    # 2. 已提交但状态不对的目标：恢复或移除
    for dst in order:
        rec = commits[dst]
        dst_path = Path(dst)
        backup = rec.get("backup") or ""
        expected = rec.get("sha256") or ""
        healthy = dst_path.is_file()
        if healthy and expected:
            try:
                healthy = sha256_file(dst_path) == expected
            except OSError:
                healthy = False
        if healthy:
            continue
        if backup and Path(backup).is_file():
            shutil.copy2(backup, dst_path)
            actions.append(f"已从备份恢复 {dst_path.name}")
        elif dst_path.exists():
            # 无备份的 commit = 导入前该文件不存在；恢复到「不存在」即删除
            dst_path.unlink()
            actions.append(f"已删除导入产生但已损坏的 {dst_path.name}（导入前不存在）")
        else:
            actions.append(f"{dst_path.name} 缺失且无备份（导入未完成）")
    return actions
