"""引擎 CLI：所有子命令的统一入口。

约定：
- 每个子命令构建并返回 Report；main 统一 emit。人看中文短表 / --json 单对象。
- --root 与 --json 放在子命令前后都可以（SUPPRESS 默认值，子解析器不覆盖外层）。
- 口令：交互 stdin 不回显；或环境变量；或 --passphrase-file。禁 argv 明文。
- 退出码契约见 migrate_engine/__init__.py，.cmd 启动器据此分支。
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

from . import (
    EXIT_CONFLICTS,
    EXIT_ERROR,
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_UNSUPPORTED,
    EXIT_USAGE,
    __version__,
)
from .manifest import (
    MANIFEST_REL,
    ManifestError,
    find_manifest,
    load_manifest,
    scaffold_manifest,
    select_items,
)
from . import platform as plat
from .crypto import CryptoError
from .env_audit import audit as audit_env
from .passphrase import PassphraseAborted, obtain as obtain_passphrase
from .pack import EnvHealthError, ExportError, KIND_WORKSPACE, export_package
from .report import Report, human_size, sha8
from .scan import _ARTIFACT_DIR_NAMES, _SKIP_DIRS, resolve_item, snapshot_files
from .server import ServerError, build_server_package, capture_server, upload_stage, validate_target
from .unpack import (
    ON_CONFLICT_MODES,
    PackageError,
    open_package,
    restore_package,
)

# ── 启发式分类（仅用于 plan 的建议区；显式 manifest 永远压过启发式） ──────

_HINTS: list[tuple[str, str, str]] = [
    # (glob, 建议类别, 理由)
    (".env", "required", "环境变量主文件，缺失通常起不来"),
    (".env.*", "recommended", "环境变量变体"),
    ("*.pem", "required", "密钥材料"),
    ("*.key", "required", "密钥材料"),
    ("*.p12", "required", "密钥材料"),
    ("*.pfx", "required", "密钥材料"),
    ("secrets.json", "required", "密钥材料"),
    ("credentials.json", "required", "密钥材料"),
    ("token.txt", "required", "令牌文件"),
    ("config.local.json", "recommended", "本地覆盖配置"),
    ("config.local.*", "recommended", "本地覆盖配置"),
    ("config.production.*", "recommended", "生产覆盖配置"),
    ("*.local.json", "recommended", "本地覆盖配置"),
    ("*.db", "recommended", "应用数据库（建议 item_type=sqlite）"),
    ("*.sqlite", "recommended", "应用数据库（建议 item_type=sqlite）"),
    ("*.sqlite3", "recommended", "应用数据库（建议 item_type=sqlite）"),
    ("*.xlsx", "recommended", "业务表格"),
    ("*.csv", "recommended", "业务表格（测试夹具除外）"),
    ("*.ods", "recommended", "业务表格"),
    ("_*.md", "recommended", "运维/交接笔记"),
    ("backups/*", "recommended", "备份快照（大目录只收最新一份）"),
]


def _hint_class(relpath: str) -> tuple[str, str] | None:
    import fnmatch

    for pattern, cls, why in _HINTS:
        if fnmatch.fnmatch(relpath, pattern):
            return cls, why
    return None


def _read_line(prompt: str) -> str | None:
    """读一行输入。没有输入流时返回 None（调用方当中止处理）。

    刻意不用裸 input()：它在 stdin 是管道/重定向时抛 EOFError，而调用方更该
    防的是 readline 返回空串导致的死循环。返回 None 一律当中止，绝不当成
    空回答再问一遍（Git Bash 里 getpass 也曾因 /dev/tty 不存在挂死，
    见 passphrase.py 的说明——那条坑这里同样适用）。
    """
    if not os.isatty(0):
        print(prompt, end="", file=sys.stderr, flush=True)
        line = sys.stdin.readline()
        if line == "":
            return None
        return line.rstrip("\r\n")
    try:
        return input(prompt).rstrip("\r\n")
    except EOFError:
        return None


def _root_of(args) -> Path:
    root = Path(getattr(args, "root", ".")).expanduser()
    if not root.is_dir():
        raise ManifestError(f"--root 不是目录：{root}", exit_code=EXIT_USAGE)
    return root.resolve()


# ── 各子命令（返回 Report） ─────────────────────────────────────────────


def cmd_doctor(args) -> Report:
    rep = Report(json_mode=bool(getattr(args, "json", False)), title="one-click-migrate 能力体检")
    caps = plat.doctor()
    rep.set_data("capabilities", caps)
    for key, value in caps.items():
        rep.say(f"  {key}：{value if value is not None else '不可用'}")
    if not caps["python_ok"]:
        rep.error(f"Python 版本过低（{caps['python']}），引擎需要 3.11+（tomllib）。")
        rep.finish(EXIT_UNSUPPORTED)
        return rep
    if caps["git"] is None:
        rep.warn("没有 git：扫描忽略项与 git 已跟踪判断不可用，manifest 仍可工作。")
    if caps["sqlite3"] is None:
        rep.warn("sqlite3 不可用：item_type=sqlite 的快照与 sidecar 清理不可用。")
    try:
        root = _root_of(args)
    except ManifestError:
        root = None
    manifest_path = find_manifest(root) if root else None
    rep.set_data("manifest", str(manifest_path) if manifest_path else None)
    rep.say(f"  manifest：{manifest_path if manifest_path else '未找到 ' + MANIFEST_REL}")
    rep.say(f"  引擎版本：{__version__}")
    rep.finish(EXIT_OK)
    return rep


def cmd_manifest_init(args) -> Report:
    root = _root_of(args)
    text = scaffold_manifest(args.project)
    target = root / MANIFEST_REL
    rep = Report(json_mode=bool(getattr(args, "json", False)), title="生成 manifest 草稿")
    if target.exists() and not args.force:
        rep.error(f"{target} 已存在。加 --force 覆盖，或先 manifest validate 校验现有文件。")
        rep.finish(EXIT_CONFLICTS)
        return rep
    if args.print_only:
        rep.say(text)
        rep.finish(EXIT_OK)
        return rep
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    rep.set_data("written", str(target))
    rep.say(f"已写入 {target}")
    rep.say("下一步：逐项确认条目（class/item_type/on_missing），再跑 migrate plan 看解析结果。")
    rep.finish(EXIT_OK)
    return rep


def cmd_manifest_validate(args) -> Report:
    root = _root_of(args)
    rep = Report(json_mode=bool(getattr(args, "json", False)), title="校验 manifest")
    try:
        manifest = load_manifest(root)
    except ManifestError as exc:
        rep.error(str(exc))
        rep.finish(exc.exit_code)
        return rep
    rep.set_data("project", manifest.project)
    rep.set_data("item_count", len(manifest.items))
    rep.set_data("profiles", sorted(manifest.profiles))
    rep.say(f"  project：{manifest.project}")
    rep.say(f"  条目数：{len(manifest.items)}")
    rep.say(f"  profiles：{', '.join(sorted(manifest.profiles)) or '（无）'}")
    rep.say(f"  产物目录：{manifest.artifacts_dir}")
    rep.finish(EXIT_OK)
    return rep


def cmd_plan(args) -> Report:
    root = _root_of(args)
    rep = Report(json_mode=bool(getattr(args, "json", False)),
                 title="迁移计划（不打包，只列清单与差异）")
    try:
        manifest = load_manifest(root)
        items = select_items(manifest, args.profile)
    except ManifestError as exc:
        rep.error(str(exc))
        rep.finish(exc.exit_code)
        return rep

    tracked = plat.git_tracked_files(root)
    ignored = plat.git_ignored_files(root)
    covered: set[str] = set()
    blocking_missing = False

    for item in items:
        if item.scope == "server":
            rep.say(f"  [服务器条目] {item.id}（{item.remote_path or item.path}）不在本机解析，"
                    "采集用 server export。")
            continue
        resolved = resolve_item(root, item, manifest.artifacts_dir)
        if resolved.missing:
            rep.row(id=item.id, cls=item.cls, size="-",
                    result="缺失，将阻断导出" if item.on_missing == "block" else "缺失，仅警告",
                    note=item.note)
            rep.warn(f"{item.id}（{item.path}）不存在。")
            if item.on_missing == "block":
                blocking_missing = True
            continue
        snaps = snapshot_files(resolved, root)
        covered.update(s.relpath for s in snaps)
        total = sum(s.bytes for s in snaps)
        digest = snaps[0].sha256 if len(snaps) == 1 else None
        rep.row(
            id=item.id, cls=item.cls, size=human_size(total),
            sha256_8=sha8(digest), result=f"{len(snaps)} 个文件",
            note="；".join(resolved.notes) or item.note,
        )
        if tracked is not None:
            for s in snaps:
                if s.relpath in tracked:
                    rep.warn(f"{item.id}: {s.relpath} 已被 git 跟踪——导入时会跳过，请确认清单没写错。")
        rep.set_data("item:" + item.id, {
            "files": [s.relpath for s in snaps],
            "bytes": total,
            "sha256": {s.relpath: s.sha256 for s in snaps},
        })

    # 启发式建议区：被忽略、未被 manifest 覆盖、命中已知形态的候选
    if ignored is not None:
        suggestions: list[str] = []
        for rel in ignored:
            if rel in covered:
                continue
            # 依赖/缓存/构建产物里的同名文件不是候选：.venv 里的 cacert.pem 是
            # certifi 自带的公钥证书，按「*.pem = 密钥材料」报出来只会误导用户
            # 把第三方包的内容搬进迁移包。
            parts = rel.split("/")
            if any(p in _SKIP_DIRS or p in _ARTIFACT_DIR_NAMES for p in parts[:-1]):
                continue
            hint = _hint_class(rel)
            if hint is None:
                continue
            cls, why = hint
            suggestions.append(f"{rel} → {cls}（{why}）")
        rep.set_data("suggestions", suggestions)
        if suggestions:
            rep.say("")
            rep.say("被忽略但未在 manifest 中的候选（确认后加为 [[items]]）：")
            rep.lines.extend(f"  [候选] {s}" for s in suggestions[:40])
            if len(suggestions) > 40:
                rep.say(f"  ……另有 {len(suggestions) - 40} 条")
    else:
        rep.say("")
        rep.say("（非 git 仓库或无 git：跳过忽略项扫描，仅解析 manifest。）")

    if blocking_missing:
        rep.error("存在缺失的必需项，现在导出会失败。")
        rep.finish(EXIT_CONFLICTS)
        return rep
    rep.finish(EXIT_OK)
    return rep


# ── export / verify / import ────────────────────────────────────────────


def cmd_export(args) -> Report:
    rep = Report(json_mode=bool(getattr(args, "json", False)),
                 title="打包本机资产（workspace）")
    root = _root_of(args)
    try:
        manifest = load_manifest(root)
        items = [i for i in select_items(manifest, args.profile)
                 if i.scope in {"workspace", "both"}]
        if not items:
            rep.error("manifest 里没有 scope=workspace/both 的条目。")
            rep.finish(EXIT_USAGE)
            return rep
        passphrase = obtain_passphrase(confirm=True, source_file=args.passphrase_file)
    except ManifestError as exc:
        rep.error(str(exc))
        rep.finish(exc.exit_code)
        return rep
    except PassphraseAborted as exc:
        rep.say(str(exc))
        rep.finish(EXIT_REFUSED)
        return rep

    try:
        result = export_package(root, manifest, items, kind=KIND_WORKSPACE,
                                passphrase=passphrase)
    except (ExportError, PassphraseAborted) as exc:
        rep.error(str(exc))
        rep.finish(getattr(exc, "exit_code", EXIT_ERROR))
        return rep
    except EnvHealthError as exc:
        rep.error("拒绝打包一个带病的 .env——它到新机器上会以看似无关的错误爆出来。")
        rep.lines.extend(f"  {p}" for p in exc.problems)
        rep.say("")
        rep.say("  先按上面修好 .env 再导。修完建议跑一次项目的健康检查，"
                "确认解析出来的键数没变。")
        rep.finish(exc.exit_code)
        return rep

    rep.set_data("package", str(result.path))
    rep.set_data("kind", result.kind)
    rep.say(f"  产物：{result.path}（{human_size(result.bytes)}，已加密）")
    for name in result.missing:
        rep.warn(f"{name} 不存在，未进包（on_missing 非 block）。")

    # 产物目录必须在忽略规则里：里面是加密包 + 导入时备份的明文 .env/私钥。
    # 曾经的事故——两个 .enc 躺在仓库根而 .gitignore 什么都没写，一次
    # git add -A 就把加密的密钥+数据库提交上去。检查比事后提醒可靠。
    ignored = plat.git_ignore_check(root, manifest.artifacts_dir)
    rep.set_data("artifacts_ignored", ignored)
    if ignored is False:
        rep.error(
            f"产物目录 {manifest.artifacts_dir}/ 没有被 .gitignore 排除——"
            "它里面是加密迁移包，导入时还会备份明文 .env 与私钥。"
            f"现在改 .gitignore（加一行 `{manifest.artifacts_dir}/`），"
            "并确认已有的包没有被 git 跟踪。"
        )
        rep.finish(EXIT_REFUSED)
        return rep
    if ignored is None:
        rep.warn(f"无法确认 {manifest.artifacts_dir}/ 是否被忽略规则覆盖"
                 "（非 git 仓库或无 git）。请自行确认它不会被提交。")

    # 导出后键名审计：本机包里「缺什么、什么是死键、哪些只应在服务器上」
    if manifest.env_audit:
        # env_files 支持多源：智能询价有两套配置（.env 本地 / .env.server 部署），
        # 只读 .env 会让只存在于 .env.server 的键被误判成「缺失」，
        # 而那正是重建服务器时静默丢功能的那一类键。
        env_files = list(manifest.env_audit.get("env_files") or [".env"])
        result_audit = audit_env(root, manifest.env_audit, env_files=env_files)
        rep.set_data("audit_missing", sorted(result_audit.missing))
        rep.set_data("audit_dead", sorted(result_audit.dead))
        rep.set_data("audit_env_files", env_files)
        if result_audit.scan_failed:
            rep.warn("环境审计没扫到代码里的变量读取（code_dirs？）——"
                     "本次不报缺失、也不报死键（扫不到时下的判断会误导）。")
        for key in sorted(result_audit.missing):
            rep.warn(f"代码读了 {key} 但 {', '.join(env_files)} 里没配——"
                     "只换电脑可以不带，重建服务器前必须补。")
        for key in sorted(result_audit.dead):
            rep.warn(f"{key} 配了但没代码读（死键，确认后可删）。")
        for key in sorted(result_audit.server_only):
            rep.say(f"  [提醒] {key} 只应在部署机上；本机 env 文件里也有，注意别外发。")

    rep.say("")
    rep.say(f"  包内 {len(result.entries)} 个文件。口令另行保管（丢了包永远解不开），")
    rep.say("  不要把口令和包放同一处。")
    rep.say("")
    rep.say("下一步：把 .enc 拷到新电脑 → git clone 项目 → migrate import <包>")
    rep.finish(EXIT_OK)
    return rep


def cmd_verify(args) -> Report:
    rep = Report(json_mode=bool(getattr(args, "json", False)), title="校验迁移包（不落盘）")
    path = Path(args.package).expanduser()
    if not path.is_file():
        rep.error(f"找不到迁移包：{path}")
        rep.finish(EXIT_USAGE)
        return rep
    try:
        passphrase = obtain_passphrase(confirm=False, source_file=args.passphrase_file,
                                       enforce_min=False)
    except PassphraseAborted as exc:
        rep.say(str(exc))
        rep.finish(EXIT_REFUSED)
        return rep
    try:
        info, _files = open_package(path, passphrase)
    except (PackageError, CryptoError, PassphraseAborted) as exc:
        rep.error(str(exc))
        rep.finish(getattr(exc, "exit_code", EXIT_ERROR))
        return rep
    rep.set_data("kind", info["kind"])
    rep.set_data("project", info.get("project"))
    rep.set_data("created_at", info.get("created_at"))
    if info.get("legacy"):
        rep.say("  ⚠ 旧格式包：无 HMAC 认证、无包内清单，仅成员预检 + 逐文件 sha256。")
    rep.say(f"  类型：{info['kind']}    项目：{info.get('project')}")
    rep.say(f"  创建：{info.get('created_at')}    引擎：{info.get('engine')}")
    rep.say(f"  清单 {len(info['items'])} 项，逐文件 sha256 校验通过。")
    for record in info["items"]:
        rep.row(id=record["id"], cls=record.get("class", "-"),
                size=human_size(record["bytes"]), sha256_8=sha8(record["sha256"]),
                result="校验通过", note=record["path"])
    rep.finish(EXIT_OK)
    return rep


def cmd_import(args) -> Report:
    rep = Report(json_mode=bool(getattr(args, "json", False)), title="导入迁移包")
    if args.on_conflict not in ON_CONFLICT_MODES:
        rep.error(f"--on-conflict 必须是 {ON_CONFLICT_MODES} 之一")
        rep.finish(EXIT_USAGE)
        return rep

    if args.pick:
        # 不指定包路径时，列出产物目录里现成的包让用户选一个。
        # 这是双击 .cmd 那类「一键换机」入口的必需品：用户手上只有一个目录，
        # 不是一条路径。选包的逻辑必须在引擎里做完，不能靠 shell 回传值——
        # 用 for /f 捕获路径曾经被 PowerShell 管道送来的 BOM 弄坏过。
        root = _root_of(args)
        manifest = None
        try:
            manifest = load_manifest(root)
        except ManifestError:
            pass
        artifacts = root / (manifest.artifacts_dir if manifest else ".migrate")
        candidates = sorted(p for p in artifacts.glob("*.enc") if p.is_file())
        if not candidates:
            rep.error(f"{artifacts} 下没有迁移包（*.enc）。先在一台旧机器上跑 export。")
            rep.finish(EXIT_USAGE)
            return rep
        rep.say(f"  {artifacts} 里有 {len(candidates)} 个迁移包：")
        for i, p in enumerate(candidates, 1):
            rep.say(f"    {i}) {p.name}  ({human_size(p.stat().st_size)}, "
                    f"{datetime.fromtimestamp(p.stat().st_mtime).strftime('%Y-%m-%d %H:%M')})")
        answer = _read_line("导入哪一个？[编号，回车取最新] ")
        if answer is None:
            rep.say("[中止] 没有输入流。请在终端里直接运行，或把包路径作为参数传入。")
            rep.finish(EXIT_REFUSED)
            return rep
        answer = answer.strip()
        if not answer:
            args.package = str(candidates[-1])
        elif answer.isdigit() and 1 <= int(answer) <= len(candidates):
            args.package = str(candidates[int(answer) - 1])
        else:
            chosen = Path(answer).expanduser()
            args.package = str(chosen)
        rep.say("")
    elif not args.package:
        rep.error("没给迁移包路径。用法：migrate import <包>，或用 --pick 从产物目录里选。")
        rep.finish(EXIT_USAGE)
        return rep

    if args.recover:
        # 崩溃恢复：重放 journal，与「导入哪个包」无关。
        # 旧接口把必填的 package 当 journal 路径用，传 .enc 会 UnicodeDecodeError
        # 裸崩、传错则假报成功——而 --recover 正是导入失败后最需要它的那一刻。
        from .journal import recover

        root = _root_of(args)
        manifest = None
        try:
            manifest = load_manifest(root)
        except ManifestError:
            pass
        artifacts = root / (manifest.artifacts_dir if manifest else ".migrate")
        journal = Path(args.journal) if args.journal else None
        if journal is None:
            candidates = sorted((artifacts / "journal").glob("import-*.jsonl"))
            if not candidates:
                rep.error(f"没找到 journal：{artifacts / 'journal'} 下没有 import-*.jsonl。")
                rep.finish(EXIT_USAGE)
                return rep
            journal = candidates[-1]        # 最新的一个
        if not journal.is_file():
            rep.error(f"找不到 journal 文件：{journal}")
            rep.finish(EXIT_USAGE)
            return rep
        rep.say(f"  重放 journal：{journal}")
        actions = recover(artifacts, journal)
        rep.say("  重放完成：")
        rep.lines.extend(f"    - {a}" for a in actions)
        rep.finish(EXIT_OK)
        return rep

    path = Path(args.package).expanduser()
    if not path.is_file():
        rep.error(f"找不到迁移包：{path}")
        rep.finish(EXIT_USAGE)
        return rep
    try:
        passphrase = obtain_passphrase(confirm=False, source_file=args.passphrase_file,
                                       enforce_min=False)
    except PassphraseAborted as exc:
        rep.say(str(exc))
        rep.finish(EXIT_REFUSED)
        return rep

    root = Path(args.out).expanduser().resolve() if args.out else _root_of(args)
    artifacts_dir: Path | None = None
    whitelist: list[str] | None = None
    try:
        manifest = load_manifest(root)
        artifacts_dir = root / manifest.artifacts_dir
        # manifest 声明的白名单必须原样生效——包括「一条都不匹配」的情形。
        # 曾经的 `or None` 会把「配了但没匹配项」变成「无限制」，于是白名单
        # 静默放开（merge.include 拼错、条目路径变更，都走这条静默失效路径）。
        # 只有 manifest 整个缺失时才没有白名单，由 tracked 兜底。
        whitelist = list(manifest.merge_include)
        if "merge" not in manifest.raw:
            # manifest 里没有 [merge] 节 → 不设白名单（裸声明 / 老模板）。
            # 有 [merge] 但 include 为空 = 「什么都不许合并」，那是用户的选择，
            # 不能悄悄变成无限制。
            whitelist = None
    except ManifestError:
        manifest = None
        whitelist = None   # 裸克隆：无白名单，由 git tracked 判断兜底
        artifacts_dir = None

    def ask_fn(name: str, dst: Path) -> str:
        print(f"  目标已存在：{name}（{human_size(dst.stat().st_size)}）")
        answer = input("  覆盖 / 跳过 / 保留双方 [overwrite/skip/keep-both，默认 skip]: ").strip()
        return answer or "skip"

    try:
        info, plan, report = restore_package(
            path, passphrase, root,
            on_conflict=args.on_conflict,
            artifacts_dir=artifacts_dir,
            merge_whitelist=whitelist,
            tracked=plat.git_tracked_files(root),
            ask_fn=ask_fn if args.on_conflict == "ask" else None,
            dry_run=args.dry_run,
        )
    except (PackageError, CryptoError) as exc:
        rep.error(str(exc))
        rep.finish(exc.exit_code)
        return rep
    except EnvHealthError as exc:
        # 坏 .env 一个字节都不落盘——写下去了，用户会以为资产已恢复
        rep.error("拒绝导入一个带病的 .env（目标没有任何改动）：")
        rep.lines.extend(f"  {p}" for p in exc.problems)
        rep.finish(exc.exit_code)
        return rep
    except PassphraseAborted as exc:
        rep.error(str(exc))
        rep.finish(EXIT_REFUSED)
        return rep

    for item in plan:
        rep.row(id=item.name, cls=info_lookup(info, item.name), size="-",
                result=item.action, note=item.reason or "")

    if args.dry_run:
        rep.say("")
        rep.say("  --dry-run：以上为计划，未写入任何文件。")
        rep.finish(EXIT_OK)
        return rep

    if report is None:  # 防御：非 dry-run 必须有 report
        rep.error("内部错误：导入未产生报告。")
        rep.finish(EXIT_ERROR)
        return rep

    conflicts = [i for i in plan if i.action == "skip" and i.reason == "目标已存在"]
    rep.say("")
    rep.say(f"  写入 {len(report.written)}，保留双方 {len(report.kept_both)}，"
            f"跳过 {len(report.skipped)}。")
    if report.backups:
        rep.say(f"  旧文件备份：{report.backups}")
    rep.say(f"  事务日志：{report.journal}（异常中断后可用 --recover 重放）")
    for warning in report.warnings:
        rep.warn(warning)
    for step in info.get("rebuild", []):
        rep.say(f"  重建：{step}")
    for step in info.get("verify", []):
        rep.say(f"  验证：{step}")
    rep.say("  验证命令失败就停，不要重复导入。")

    if not report.written and not report.kept_both:
        # 一个文件都没落地却报成功，是「换机后发现环境没恢复」的直接原因。
        # 旧逻辑只在 reason == 「目标已存在」时给 3，blocked（git 已跟踪 /
        # 不在白名单）算成功——于是 tracked 数据文件全被跳过时静默空转。
        blocked_by_tracked = [i.name for i in plan
                              if i.action == "blocked" and "git" in i.reason]
        blocked_by_whitelist = [i.name for i in plan
                                if i.action == "blocked" and "白名单" in i.reason]
        rep.error("一个文件都没有写入——换机资产没有恢复。")
        if blocked_by_tracked:
            rep.warn(f"{len(blocked_by_tracked)} 个文件已被 git 跟踪（以仓库为准）："
                     + ", ".join(blocked_by_tracked[:10]))
            rep.say("  这些文件不在迁移包里恢复，改它们请 commit/push，或把它们从"
                    " .gitignore 放出来后再打包。")
        if blocked_by_whitelist:
            rep.warn(f"{len(blocked_by_whitelist)} 个文件不在 manifest 的 merge.include 白名单："
                     + ", ".join(blocked_by_whitelist[:10]))
            rep.say("  要在白名单里补上对应路径（目录写目录名即可，支持前缀与 glob）。")
        if not blocked_by_tracked and not blocked_by_whitelist:
            rep.warn(f"{len(report.skipped)} 个文件目标已存在被跳过；确认覆盖加 "
                     "--on-conflict overwrite（旧文件会先备份）。")
        rep.finish(EXIT_CONFLICTS)
        return rep

    if conflicts and args.on_conflict == "skip":
        # 退出码 3：.cmd 启动器据此提供 --overwrite 重试；错密码/服务在跑不提供
        rep.warn(f"{len(conflicts)} 个文件目标已存在被跳过；确认覆盖加 --on-conflict overwrite"
                 "（旧文件会先备份）。")
        rep.finish(EXIT_CONFLICTS)
        return rep
    rep.finish(EXIT_OK)
    return rep


def info_lookup(info: dict, name: str) -> str:
    for record in info["items"]:
        if record["path"] == name:
            return record.get("class", "-")
    return "-"


# ── audit / server ──────────────────────────────────────────────────────


def cmd_audit(args) -> Report:
    """环境变量键名审计：只出键名，绝不出值。"""
    rep = Report(json_mode=bool(getattr(args, "json", False)), title="环境变量键名审计")
    root = _root_of(args)
    try:
        manifest = load_manifest(root)
    except ManifestError as exc:
        rep.error(str(exc))
        rep.finish(exc.exit_code)
        return rep
    env_files = list(manifest.env_audit.get("env_files") or [".env"])
    result = audit_env(root, manifest.env_audit, env_files=env_files)
    rep.set_data("missing", sorted(result.missing))
    rep.set_data("missing_required", sorted(result.missing_required))
    rep.set_data("dead", sorted(result.dead))
    rep.set_data("server_only_declared", sorted(result.server_only))
    rep.set_data("env_files", env_files)
    if result.scan_failed:
        rep.error("没扫到任何代码里的环境变量读取——code_dirs 配错了？"
                  "fail-closed：不报「正常」，也不报缺失/死键（那会误导）。")
        rep.finish(EXIT_ERROR)
        return rep
    for key in sorted(result.missing_required):
        rep.error(f"缺少生产必需的环境变量：{key}")
    for key in sorted(result.missing - result.missing_required):
        rep.warn(f"代码读了 {key}，但 {', '.join(env_files)} 与部署机清单里都没有——换机会缺它。")
    for key in sorted(result.dead):
        rep.warn(f"{key} 配了但没代码读——死键，确认后可删。")
    for key in sorted(result.server_only):
        rep.say(f"  [提醒] {key} 只应在部署机上（本机 env 文件里也有，注意别外发）。")
    rep.say(f"  代码读取 {len(result.code_keys)} 个键；env 文件 {len(result.env_keys)} 个键"
            f"（{', '.join(env_files)}）。")
    if not (result.missing or result.dead):
        rep.say("  没有缺失、没有死键。")
    rep.finish(EXIT_OK)
    return rep


def _server_hints(root: Path) -> dict[str, dict]:
    """从本机 manifest 取 server 条目的 remote_path/pg_db，供上传命令用。"""
    try:
        manifest = load_manifest(root)
    except ManifestError:
        return {}
    hints: dict[str, dict] = {}
    for item in manifest.items:
        if item.scope in {"server", "both"}:
            hints[item.path] = {"remote_path": item.remote_path, "pg_db": item.pg_db}
    return hints


def cmd_server_export(args) -> Report:
    rep = Report(json_mode=bool(getattr(args, "json", False)), title="服务器资产采集")
    root = _root_of(args)
    target = validate_target(args.target)
    try:
        manifest = load_manifest(root)
        items = [i for i in select_items(manifest, getattr(args, "profile", None))
                 if i.scope in {"server", "both"}]
        if not items:
            rep.error("manifest 里没有 scope=server/both 的条目。")
            rep.finish(EXIT_USAGE)
            return rep
        passphrase = obtain_passphrase(confirm=True, source_file=args.passphrase_file)
    except ManifestError as exc:
        rep.error(str(exc))
        rep.finish(exc.exit_code)
        return rep
    except PassphraseAborted as exc:
        rep.say(str(exc))
        rep.finish(EXIT_REFUSED)
        return rep

    try:
        result = capture_server(target, manifest, items)
        out = build_server_package(result, manifest, passphrase, root / manifest.artifacts_dir)
    except (ServerError, PassphraseAborted) as exc:
        rep.error(str(exc))
        rep.finish(getattr(exc, "exit_code", EXIT_ERROR))
        return rep

    rep.set_data("package", str(out))
    rep.set_data("target_alias", target)
    ignored = plat.git_ignore_check(root, manifest.artifacts_dir)
    rep.set_data("artifacts_ignored", ignored)
    if ignored is False:
        rep.error(
            f"产物目录 {manifest.artifacts_dir}/ 没有被 .gitignore 排除——"
            "server 包装的是数据库转储与系统配置明文密钥。先改 .gitignore 再导。"
        )
        rep.finish(EXIT_REFUSED)
        return rep
    if ignored is None:
        rep.warn(f"无法确认 {manifest.artifacts_dir}/ 是否被忽略规则覆盖"
                 "（非 git 仓库或无 git）。server 包含明文密钥，请注意落点。")
    rep.say(f"  目标：{target}")
    rep.say(f"  产物：{out}（{human_size(out.stat().st_size)}，已加密）")
    for note in result.notes:
        rep.warn(note)
    rep.say("  版本锚点（新机照齐）：")
    rep.lines.extend(f"    {line}" for line in result.metadata.splitlines() if line.strip())
    rep.say("")
    rep.say("  ⚠ 这是服务器包：含数据库转储与系统配置明文密钥，禁止合并进项目仓库。")
    rep.say("  换服务器：migrate server import <包> --target user@新机")
    rep.finish(EXIT_OK)
    return rep


def cmd_server_import(args) -> Report:
    rep = Report(json_mode=bool(getattr(args, "json", False)), title="上传到新服务器（只进暂存）")
    path = Path(args.package).expanduser()
    if not path.is_file():
        rep.error(f"找不到迁移包：{path}")
        rep.finish(EXIT_USAGE)
        return rep
    target = validate_target(args.target)
    try:
        root = _root_of(args)
    except ManifestError as exc:
        rep.error(str(exc))
        rep.finish(exc.exit_code)
        return rep
    try:
        passphrase = obtain_passphrase(confirm=False, source_file=args.passphrase_file,
                                       enforce_min=False)
    except PassphraseAborted as exc:
        rep.say(str(exc))
        rep.finish(EXIT_REFUSED)
        return rep
    try:
        commands = upload_stage(path, passphrase, target,
                                hints=_server_hints(root))
    except (ServerError, PackageError, CryptoError) as exc:
        rep.error(str(exc))
        rep.finish(getattr(exc, "exit_code", EXIT_ERROR))
        return rep
    rep.set_data("target_alias", target)
    rep.say(f"  已上传到 {target} 的暂存目录。接下来人工执行（引擎不自行提权）：")
    rep.lines.extend(f"  {cmd}" for cmd in commands)
    rep.say("")
    rep.say("  必须改的值：数据库连接串、对外访问来源（ALLOW_ORIGINS 之类）。")
    rep.say("  换了 IP/域名：已发链接全部失效，要重发（token 在库里，不用重建账号）。")
    rep.finish(EXIT_OK)
    return rep


def _cmd_server(args) -> Report:
    if args.action == "export":
        return cmd_server_export(args)
    if not args.package:
        rep = Report(json_mode=bool(getattr(args, "json", False)))
        rep.error("server import 需要迁移包路径：migrate server import <包> --target user@host")
        rep.finish(EXIT_USAGE)
        return rep
    return cmd_server_import(args)


def _cmd_manifest(args) -> Report:
    if args.action == "init":
        if not args.project:
            rep = Report(json_mode=bool(getattr(args, "json", False)))
            rep.error("manifest init 需要 --project <slug>")
            rep.finish(EXIT_USAGE)
            return rep
        return cmd_manifest_init(args)
    return cmd_manifest_validate(args)


# ── 参数解析 ────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    # SUPPRESS 默认值：--root/--json 放子命令前后均可，子解析器不会覆盖外层已解析值。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=argparse.SUPPRESS, help="项目根目录（默认当前目录）")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="机器可读输出（单对象 JSON）")

    parser = argparse.ArgumentParser(
        prog="migrate",
        description="一键换机：识别并迁移被 git 排除、但运行必要的数据",
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", parents=[common], help="能力体检（平台/依赖/manifest）")

    p_init = sub.add_parser("manifest", parents=[common], help="manifest 草稿生成与校验")
    p_init.add_argument("action", choices=["init", "validate"])
    p_init.add_argument("--project", default=None, help="项目 slug（init 必填）")
    p_init.add_argument("--force", action="store_true", help="覆盖已存在的 manifest")
    p_init.add_argument("--print", action="store_true", dest="print_only", help="只打印不落盘")

    p_plan = sub.add_parser("plan", parents=[common], help="迁移计划：解析清单 + 忽略项差异，不打包")
    p_plan.add_argument("--profile", default=None, help="选择 profiles 中的配置集")

    p_export = sub.add_parser("export", parents=[common], help="打包本机资产为加密迁移包")
    p_export.add_argument("--profile", default=None, help="选择 profiles 中的配置集")
    p_export.add_argument("--passphrase-file", default=None,
                          help="从文件读口令（首行）；也可用环境变量 MIGRATE_PASSPHRASE")

    p_verify = sub.add_parser("verify", parents=[common], help="校验迁移包完整性与类型，不落盘")
    p_verify.add_argument("package", help=".enc 迁移包路径")
    p_verify.add_argument("--passphrase-file", default=None)

    p_import = sub.add_parser("import", parents=[common], help="在新机器上导入迁移包")
    p_import.add_argument("package", nargs="?", default=None, help=".enc 迁移包路径（--recover 时可不给）")
    p_import.add_argument("--out", default=None, help="目标项目根（默认 --root）")
    p_import.add_argument("--on-conflict", default="skip", choices=list(ON_CONFLICT_MODES),
                          help="目标已存在时的处理（默认 skip：只写不存在的文件）")
    p_import.add_argument("--dry-run", action="store_true", help="只打印计划，不写任何文件")
    p_import.add_argument("--pick", action="store_true",
                          help="不给包路径时，列出产物目录里的包让用户选")
    p_import.add_argument("--recover", action="store_true",
                          help="重放 journal，恢复中断的导入（不带包路径）")
    p_import.add_argument("--journal", default=None,
                          help="--recover 时指定 journal 文件（默认取最新一个）")
    p_import.add_argument("--passphrase-file", default=None)

    p_audit = sub.add_parser("audit", parents=[common], help="环境变量键名审计（只出键名）")

    p_server = sub.add_parser("server", parents=[common], help="服务器资产采集/上传")
    p_server.add_argument("action", choices=["export", "import"])
    p_server.add_argument("--target", required=True, help="user@host（地址不进仓库）")
    p_server.add_argument("--profile", default=argparse.SUPPRESS,
                          help="选择 profiles 中的配置集")
    p_server.add_argument("package", nargs="?", default=None,
                          help="server import 时的 .enc 包路径")
    p_server.add_argument("--passphrase-file", default=None)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    json_mode = bool(getattr(args, "json", False))
    handlers = {
        "doctor": cmd_doctor,
        "manifest": _cmd_manifest,
        "plan": cmd_plan,
        "export": cmd_export,
        "verify": cmd_verify,
        "import": cmd_import,
        "audit": cmd_audit,
        "server": _cmd_server,
    }
    try:
        report = handlers[args.cmd](args)
    except ManifestError as exc:
        report = Report(json_mode=json_mode)
        report.error(str(exc))
        report.finish(exc.exit_code)
    except KeyboardInterrupt:
        print("\n[中止] 用户中断。", file=sys.stderr)
        return EXIT_REFUSED
    except (OSError, ValueError, TimeoutError) as exc:
        # 平台层与解析层的意外（ssh 超时、文件被占用、URI 解析失败）不该把
        # Python traceback 摔给用户——那不是可操作的错误信息。
        report = Report(json_mode=json_mode)
        report.error(f"{type(exc).__name__}: {exc}")
        report.finish(EXIT_ERROR)
    return report.emit()


if __name__ == "__main__":
    raise SystemExit(main())
