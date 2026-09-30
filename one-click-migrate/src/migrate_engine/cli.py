"""引擎 CLI：所有子命令的统一入口。

约定：
- 每个子命令构建并返回 Report；main 统一 emit。人看中文短表 / --json 单对象。
- --root 与 --json 放在子命令前后都可以（SUPPRESS 默认值，子解析器不覆盖外层）。
- 口令：交互 stdin 不回显；或环境变量；或 --passphrase-file。禁 argv 明文。
- 退出码契约见 migrate_engine/__init__.py，.cmd 启动器据此分支。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import (
    EXIT_CONFLICTS,
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
from .report import Report, human_size, sha8
from .scan import resolve_item, snapshot_files

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
        resolved = resolve_item(root, item)
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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    json_mode = bool(getattr(args, "json", False))
    handlers = {
        "doctor": cmd_doctor,
        "manifest": _cmd_manifest,
        "plan": cmd_plan,
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
    return report.emit()


if __name__ == "__main__":
    raise SystemExit(main())
