"""导入与校验：成员预检 → 冲突决策 → 事务式写入。

两道关卡：
1. 成员预检在提取前完成：名字、类型、限额全部合法才允许进入写阶段。
2. 事务式写入：备份 → 临时文件 → sha256 → 原子替换；任一步失败即回滚
   本次导入已落盘的文件（有备份从备份恢复，无备份删除），并留 journal 供
   `import --recover` 崩溃恢复。

不变量：kind == server 的包禁止合并进 git 工作区。
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import stat
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import EXIT_INTEGRITY, EXIT_REFUSED
from .crypto import CryptoError, decrypt_blob
from .env_health import check_env_health
from .journal import Journal, atomic_commit, journal_path
from .pack import KIND_SERVER, MAX_FILE_BYTES, MAX_MEMBERS, PACKAGE_FORMAT
from .scan import sha256_file

SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


class PackageError(Exception):
    def __init__(self, message: str, *, exit_code: int = EXIT_INTEGRITY):
        super().__init__(message)
        self.exit_code = exit_code


# ── 关卡一：打开与成员预检 ──────────────────────────────────────────────


def check_member_name(name: str) -> str | None:
    """返回问题描述；None 表示合法。"""
    if not name or name.endswith("/"):
        return "空成员名/目录条目"
    if "\\" in name:
        return f"含反斜杠（拒绝 Windows 路径形态）：{name}"
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        return f"绝对路径/盘符：{name}"
    if name.startswith("//"):
        return f"UNC 路径：{name}"
    parts = name.split("/")
    if ".." in parts or "." in parts:
        return f"含 . / .. 段：{name}"
    if any(part == "" for part in parts[:-1]):
        return f"空路径段：{name}"
    if "\x00" in name:
        return f"含 NUL：{name}"
    return None


def open_package(path: Path, passphrase: str) -> tuple[dict, dict[str, tuple[bytes, str]]]:
    """解密并打开包。返回 (package_manifest, {name: (data, sha256)})。

    旧格式包（openssl Salted__ 头）走 legacy 只读路径；新包走 OCMIG1。
    成员预检在这里完成：任何非法成员在任何字节落盘前就失败。
    """
    from .legacy import is_legacy_blob

    blob_head = path.read_bytes()[:8]
    if is_legacy_blob(blob_head):
        from .legacy import open_legacy_package

        return open_legacy_package(path, passphrase)

    blob = path.read_bytes()
    zip_bytes = decrypt_blob(blob, passphrase)   # 口令错/篡改 → CryptoError(exit 4)
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        raise PackageError("解密成功但不是合法 zip——包可能损坏。") from exc

    with zf:
        names = zf.namelist()
        if "manifest.json" not in names:
            raise PackageError("包里缺 manifest.json——文件可能损坏。")
        package_manifest = _parse_manifest(zf.read("manifest.json"))
        if package_manifest["format"] != PACKAGE_FORMAT:
            raise PackageError(
                f"包格式版本 {package_manifest['format']} 与引擎 {PACKAGE_FORMAT} 不一致。"
            )
        declared = {item["path"]: item for item in package_manifest["items"]}
        if len(names) > MAX_MEMBERS:
            raise PackageError(f"包内成员 {len(names)} 个，超过上限 {MAX_MEMBERS}。")
        files: dict[str, tuple[bytes, str]] = {}
        total = 0
        for info in zf.infolist():
            name = info.filename
            if name == "manifest.json":
                continue
            problem = check_member_name(name)
            if problem:
                raise PackageError(f"包内成员不安全：{problem}")
            if info.is_dir():
                continue
            mode = info.external_attr >> 16
            if mode and stat.S_ISLNK(mode):
                raise PackageError(f"包内含符号链接成员（拒绝）：{name}")
            if info.file_size > MAX_FILE_BYTES:
                raise PackageError(f"成员超过单文件上限（{info.file_size} > {MAX_FILE_BYTES}）：{name}")
            total += info.file_size
            data = zf.read(name)
            digest = hashlib.sha256(data).hexdigest()
            record = declared.get(name)
            if record is None:
                raise PackageError(f"包内文件不在 manifest 清单里：{name}")
            if digest != record["sha256"] or len(data) != record["bytes"]:
                raise PackageError(f"包内文件哈希与清单不符：{name}")
            files[name] = (data, digest)
        covered = {name for name in files}
        missing = [name for name in declared if name not in covered]
        if missing:
            raise PackageError(f"清单里有、包里没有的文件：{', '.join(sorted(missing))}")
    return package_manifest, files


def _parse_manifest(raw: bytes) -> dict:
    import json

    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PackageError(f"manifest.json 不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict) or "kind" not in data or "items" not in data:
        raise PackageError("manifest.json 缺 kind/items 字段。")
    if data["kind"] not in {"workspace", "server"}:
        raise PackageError(f"未知 kind：{data['kind']!r}")
    return data


# ── 关卡二：冲突决策 ────────────────────────────────────────────────────

ON_CONFLICT_MODES = ("skip", "ask", "keep-both", "overwrite")


@dataclass
class RestorePlanItem:
    name: str
    dst: Path
    action: str          # write | skip | keep-both | overwrite | blocked
    reason: str = ""
    item_type: str = "file"
    mode: str = ""       # 源文件权限位（八进制串）；空 = 用默认
    sensitive: bool = False


@dataclass
class RestoreReport:
    written: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    kept_both: list[str] = field(default_factory=list)
    backups: Path | None = None
    journal: Path | None = None
    warnings: list[str] = field(default_factory=list)


def _whitelisted(name: str, whitelist: list[str]) -> bool:
    """白名单匹配：逐字相等、目录前缀、或 fnmatch。

    必须支持前缀——dir/glob 条目展开出的每个子文件都要单独过这一关。
    只用全等时，manifest 写 `merge.include = ["keys"]` 会让 keys/ 下
    每一个文件都被 blocked：智能询价实测 46 个文件只能恢复 3 个，
    连 RSA 私钥都进不去。
    """
    import fnmatch

    for entry in whitelist:
        if name == entry:
            return True
        prefix = entry.rstrip("/") + "/"
        if prefix != "/" and name.startswith(prefix):
            return True
        if fnmatch.fnmatch(name, entry):
            return True
    return False


def plan_restore(root: Path, package_manifest: dict, *, on_conflict: str,
                 tracked: set[str] | None, merge_whitelist: list[str] | None,
                 ask_fn=None) -> list[RestorePlanItem]:
    """决定每个包内文件的落点动作。不写任何东西。"""
    plan: list[RestorePlanItem] = []
    for record in package_manifest["items"]:
        name = record["path"]
        dst = root / name
        item_type = record.get("item_type", "file")
        mode = record.get("mode", "") or ""
        sensitive = bool(record.get("sensitive", False))
        if merge_whitelist is not None and not _whitelisted(name, merge_whitelist):
            plan.append(RestorePlanItem(name, dst, "blocked",
                                        reason="不在 manifest 的 merge.include 白名单",
                                        item_type=item_type, mode=mode, sensitive=sensitive))
            continue
        if tracked is not None and name in tracked:
            plan.append(RestorePlanItem(name, dst, "blocked",
                                        reason="已被 git 跟踪（以仓库为准）",
                                        item_type=item_type, mode=mode, sensitive=sensitive))
            continue
        if not dst.exists():
            plan.append(RestorePlanItem(name, dst, "write", item_type=item_type,
                                        mode=mode, sensitive=sensitive))
            continue
        if on_conflict == "skip":
            plan.append(RestorePlanItem(name, dst, "skip", reason="目标已存在",
                                        item_type=item_type, mode=mode, sensitive=sensitive))
        elif on_conflict == "keep-both":
            plan.append(RestorePlanItem(name, dst, "keep-both", item_type=item_type,
                                        mode=mode, sensitive=sensitive))
        elif on_conflict == "ask":
            choice = ask_fn(name, dst) if ask_fn else "skip"
            action = {"overwrite": "overwrite", "skip": "skip",
                      "keep-both": "keep-both"}.get(choice, "skip")
            plan.append(RestorePlanItem(name, dst, action, reason="用户逐项选择",
                                        item_type=item_type, mode=mode, sensitive=sensitive))
        else:  # overwrite
            plan.append(RestorePlanItem(name, dst, "overwrite", reason="用户选择全部覆盖",
                                        item_type=item_type, mode=mode, sensitive=sensitive))
    return plan


def _apply_mode(dst: Path, mode: str, *, sensitive: bool) -> None:
    """按源权限位设权；缺失或非法时按敏感度给保守默认值。

    默认值必须是"收紧"而不是"放开"：sensitive 项取不到 mode 时给 0600，
    非敏感项给 0644。曾经的实现完全不设权，落盘权限由 umask 决定——
    keys/*.pem 从 0600 变成 globally readable，部署机上等于公开私钥。

    Windows 上 chmod 基本无效（只有只读位），失败不影响导入本身。
    """
    target: int
    try:
        target = int(mode, 8) if mode else 0
    except ValueError:
        target = 0
    if not target:
        target = 0o600 if sensitive else 0o644
    try:
        os.chmod(dst, target)
    except OSError:
        pass   # 平台不支持 / 文件系统无权限位：导入继续，不因此失败


def _backup_dir(artifacts_dir: Path) -> Path:
    d = artifacts_dir / f"backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def apply_restore(root: Path, package_manifest: dict, files: dict[str, tuple[bytes, str]], *,
                  plan: list[RestorePlanItem], artifacts_dir: Path, stamp: str) -> RestoreReport:
    """按计划写入。任一步失败即回滚本次导入已落盘的文件。"""
    report = RestoreReport()
    journal_file = journal_path(artifacts_dir, stamp)
    report.journal = journal_file
    journal = Journal(journal_file)
    backups = _backup_dir(artifacts_dir)
    report.backups = backups
    done: list[tuple[Path, Path | None]] = []   # (dst, backup or None) 用于回滚

    # 落盘前再过一次 env 健康检查：拦的是「手工拷过来的坏 .env」，
    # 与 pack 侧的检查互补（pack 拦源机器，这里拦传输环节）。
    env_problems: list[str] = []
    for item in plan:
        if item.action in {"write", "overwrite", "keep-both"}:
            base = item.name.rsplit("/", 1)[-1]
            if base == ".env" or base.startswith(".env."):
                for p in check_env_health(files[item.name][0]):
                    env_problems.append(f"{item.name}：{p}")
    if env_problems:
        journal.log(op="done", ok=False)
        journal.close()
        raise EnvHealthError(env_problems)

    try:
        # SQLite 恢复前先清目标旁的 stale sidecar（旧 WAL 帧重放 = 数据库损坏）。
        # 只对「真的要写库」的动作清：skip 的语义是「不动我的数据库」，
        # 那时删 -wal/-shm 会直接丢掉用户已提交但未 checkpoint 的帧。
        db_targets = [p.name for p in plan
                      if p.action in {"write", "overwrite", "keep-both"}
                      and p.item_type == "sqlite"]
        for name in db_targets:
            for suffix in SIDECAR_SUFFIXES:
                sidecar = root / (name + suffix)
                if sidecar.exists():
                    backup_side = backups / (name + suffix)
                    backup_side.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(sidecar, backup_side)
                    journal.log(op="backup", dst=str(sidecar), backup=str(backup_side))
                    sidecar.unlink()
                    journal.log(op="sidecar-removed", dst=str(sidecar), backup=str(backup_side))
                    report.warnings.append(f"已清除旧数据库残留 {name + suffix}（备份在 {backups.name}/）")

        for item in plan:
            dst = item.dst
            if item.action == "blocked":
                journal.log(op="skip", dst=str(dst), reason=item.reason)
                report.skipped.append(f"{item.name}（{item.reason}）")
                continue
            data, digest = files[item.name]
            if item.action == "skip":
                journal.log(op="skip", dst=str(dst), reason=item.reason)
                report.skipped.append(f"{item.name}（{item.reason}）")
                continue

            backup_path: Path | None = None
            if dst.exists() and item.action in {"overwrite", "keep-both"}:
                backup_path = backups / item.name
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dst, backup_path)
                journal.log(op="backup", dst=str(dst), backup=str(backup_path))

            final_dst = dst
            if item.action == "keep-both":
                final_dst = dst.with_name(f"{dst.name}.incoming-{stamp}")
            final_dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = final_dst.with_name(f".{final_dst.name}.tmp-{os.getpid()}-{stamp}")
            journal.log(op="write", tmp=str(tmp), dst=str(final_dst), sha256=digest)
            atomic_commit(tmp=tmp, dst=final_dst, data=data, sha256=digest)
            _apply_mode(final_dst, item.mode, sensitive=item.sensitive)
            journal.log(op="commit", dst=str(final_dst), backup=str(backup_path or ""),
                        sha256=digest)
            done.append((final_dst, backup_path))
            if item.action == "keep-both":
                report.kept_both.append(str(final_dst.name))
            else:
                report.written.append(item.name)
        journal.log(op="done", ok=True)
    except Exception:
        # 回滚：反向恢复本次已落盘的文件
        for final_dst, backup_path in reversed(done):
            try:
                if backup_path is not None and backup_path.is_file():
                    shutil.copy2(backup_path, final_dst)
                    journal.log(op="rollback", dst=str(final_dst), backup=str(backup_path))
                elif final_dst.exists():
                    final_dst.unlink()
                    journal.log(op="rollback-removed", dst=str(final_dst))
            except OSError:
                pass
        journal.log(op="done", ok=False)
        raise
    finally:
        journal.close()
    return report


def restore_package(path: Path, passphrase: str, root: Path, *, on_conflict: str = "skip",
                    artifacts_dir: Path | None = None,
                    merge_whitelist: list[str] | None = None,
                    tracked: set[str] | None = None, ask_fn=None,
                    dry_run: bool = False) -> tuple[dict, list[RestorePlanItem], RestoreReport | None]:
    """导入总入口。server 包禁止合并进 git 工作区（kind 判型，不看 merge 标志）。

    artifacts_dir 由调用方从项目 manifest 提供——导入备份与 journal 里可能有
    明文 .env / 私钥，落点必须是该项目忽略规则已经排除的目录。缺省回落
    DEFAULT_ARTIFACTS_DIR，仅用于没有 manifest 的裸克隆。

    merge_whitelist=None 表示无白名单限制（无 manifest 的裸克隆，由 tracked 兜底）
    """
    package_manifest, files = open_package(path, passphrase)
    if package_manifest["kind"] == KIND_SERVER:
        raise PackageError(
            "这是服务器资产包（含数据库转储与系统配置明文密钥），禁止合并进项目工作区。\n"
            "换服务器请用 `migrate server import`：上传到新机暂存目录，再人工确认系统路径。",
            exit_code=EXIT_REFUSED,
        )
    root = root.resolve()
    plan = plan_restore(root, package_manifest, on_conflict=on_conflict, tracked=tracked,
                        merge_whitelist=merge_whitelist, ask_fn=ask_fn)
    if dry_run:
        return package_manifest, plan, None
    from .manifest import DEFAULT_ARTIFACTS_DIR

    target_artifacts = (artifacts_dir or (root / DEFAULT_ARTIFACTS_DIR)).resolve()
    report = apply_restore(root, package_manifest, files, plan=plan,
                           artifacts_dir=target_artifacts,
                           stamp=datetime.now().strftime("%Y%m%d-%H%M%S"))
    return package_manifest, plan, report
