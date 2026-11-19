# -*- coding: utf-8 -*-
"""技能可移植性 lint —— 换机后第一道闸。

检查三类「换机即失效」的问题（都是实测踩过的家族）：
  1. 技能正文/脚本里的绝对盘符路径与旧机器名（如旧机用户目录）。
     技能必须靠环境变量或相对定位找项目，写死盘符 = 换机即死。
  2. 依赖特定解释器版本（pyc 绑定）却没在 SKILL.md 显著声明。
     版本错会静默返回空，不报错，极易误判成「没有数据」。
  3. 本机各 AI 工具的技能扫描目录是否都挂了本仓库 junction。
     漏挂 = 该工具的所有会话都看不到这些技能。

用法:
    py -3.13 skill-inventory/scripts/portability_lint.py            # 扫本仓库
    py -3.13 skill-inventory/scripts/portability_lint.py --json     # 机器可读
    py -3.13 skill-inventory/scripts/portability_lint.py --skip-mounts

退出码: 0 = 无 ERROR（WARN 仍会列出）; 1 = 有 ERROR; 2 = 参数/环境错误。
只依赖标准库。行内注释 `lint:allow-path` 可豁免单行（用于有意保留的历史示例）。
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

# 盘符路径：单个字母 + 冒号 + 斜杠。前面不能是字母/数字/下划线，
# 避免误伤 URL、时间、变量名的一部分。
DRIVE_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z]):[\\/]")
# 旧机器特征词（可按机器追加）。命中即视为机器绑定内容。
OLD_MACHINE_MARKERS = ["Ingulf"]  # lint:allow-path 特征词定义本身，不是机器绑定
# pyc / 解释器版本绑定的痕迹
PYC_RE = re.compile(r"cpython-3\d{2}|\.pycbin|\.pyc\b")
VERSION_DECL_RE = re.compile(r"3\.\d{1,2}")
VERSION_WORD_RE = re.compile(r"必须|只认|要求|版本")
ALLOW_MARKER = "lint:allow-path"

# 扫描范围：入口与可执行内容按 ERROR 计；参考文档按 WARN 计
# （references 里允许保留带日期的历史案例路径，但要点名让人知道）。
ERROR_GLOBS = ["SKILL.md", "scripts/**/*.py", "scripts/**/*.ps1",
               "scripts/**/*.sh", "scripts/**/*.cmd", "scripts/**/*.js",
               "src/**/*.py"]
WARN_GLOBS = ["references/**/*.md", "examples/**/*.md"]

MOUNT_ROOTS = [
    ".workbuddy-ai/skills",   # WorkBuddy AI
    ".codex/skills",          # Codex
    ".claude/skills",         # Claude Code
    ".agents/skills",         # 豆包 / 通用 agents 目录
]


def iter_files(skill_dir, globs):
    seen = set()
    for pattern in globs:
        for path in sorted(skill_dir.glob(pattern)):
            if path.is_file() and path not in seen:
                seen.add(path)
                yield path


def scan_skill(skill_dir):
    findings = []
    pyc_seen = False
    self_path = Path(__file__).resolve()
    for level, globs in (("ERROR", ERROR_GLOBS), ("WARN", WARN_GLOBS)):
        for path in iter_files(skill_dir, globs):
            if path.resolve() == self_path:
                continue  # 扫描器自身含特征词与正则样本，不参与被扫
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                findings.append({"level": "WARN", "file": str(path), "line": 0,
                                 "msg": "读取失败: %s" % exc})
                continue
            rel = path.relative_to(skill_dir).as_posix()
            # 版本声明检查只看可执行内容：正文里「提到」pyc 坑不算依赖 pyc
            if level == "ERROR" and PYC_RE.search(text):
                pyc_seen = True
            for lineno, line in enumerate(text.splitlines(), 1):
                if ALLOW_MARKER in line:
                    continue
                drives = DRIVE_RE.findall(line)
                markers = [m for m in OLD_MACHINE_MARKERS if m in line]
                if drives or markers:
                    bits = []
                    if drives:
                        bits.append("盘符路径 " + "/".join(sorted(set(d + ":" for d in drives))))
                    if markers:
                        bits.append("旧机器特征词 " + "/".join(markers))
                    findings.append({"level": level, "file": rel, "line": lineno,
                                     "msg": "机器绑定内容（%s）→ 改环境变量/相对定位" % "，".join(bits)})
    skill_md = skill_dir / "SKILL.md"
    if pyc_seen and skill_md.is_file():
        head = skill_md.read_text(encoding="utf-8", errors="replace")
        if not (VERSION_DECL_RE.search(head) and VERSION_WORD_RE.search(head)):
            findings.append({"level": "WARN", "file": "SKILL.md", "line": 0,
                             "msg": "依赖 pyc/特定解释器版本，但 SKILL.md 未显著声明版本约束"
                                    "（版本错会静默返回空）"})
    return findings


def check_mounts(repo, home):
    rows = []
    for rel in MOUNT_ROOTS:
        root = home / rel
        link = root / repo.name
        if not link.exists():
            rows.append({"root": str(root), "status": "missing",
                         "msg": "未挂载 → 跑 scripts/link-skills 补上"})
            continue
        try:
            real = Path(os.path.realpath(str(link)))
            same = real == repo or real == Path(os.path.realpath(str(repo)))
        except OSError:
            same = False
        if same:
            rows.append({"root": str(root), "status": "ok", "msg": "已挂载且指向本仓库"})
        else:
            rows.append({"root": str(root), "status": "foreign",
                         "msg": "存在但指向别处（%s）→ 人工核对，勿直接覆盖" % real})
    return rows


def main():
    ap = argparse.ArgumentParser(description="技能可移植性 lint（盘符硬编码/版本声明/挂载覆盖）")
    ap.add_argument("--repo", default=None, help="技能仓库根（默认按脚本位置推断）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--skip-mounts", action="store_true", help="只扫内容，不查挂载")
    args = ap.parse_args()

    repo = Path(args.repo).resolve() if args.repo else Path(__file__).resolve().parents[2]
    if not (repo / "README.md").exists():
        print("[错误] 仓库根不成立（缺 README.md）: %s" % repo, file=sys.stderr)
        return 2
    home = Path(os.path.expanduser("~"))

    skills = sorted(d for d in repo.iterdir()
                    if d.is_dir() and (d / "SKILL.md").is_file()
                    and not d.name.startswith("."))
    report = {"repo": str(repo), "skills": {}, "mounts": []}
    n_err = n_warn = 0
    for skill in skills:
        findings = scan_skill(skill)
        report["skills"][skill.name] = findings
        n_err += sum(1 for f in findings if f["level"] == "ERROR")
        n_warn += sum(1 for f in findings if f["level"] == "WARN")
    if not args.skip_mounts:
        report["mounts"] = check_mounts(repo, home)
        n_err += sum(1 for m in report["mounts"] if m["status"] in ("missing", "foreign"))

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("技能可移植性 lint — %s" % repo)
        print("=" * 60)
        for name, findings in report["skills"].items():
            if not findings:
                print("[干净] %s" % name)
                continue
            print("[%s] %s" % ("问题" if any(f["level"] == "ERROR" for f in findings) else "提醒", name))
            for f in findings:
                loc = "%s:%s" % (f["file"], f["line"]) if f["line"] else f["file"]
                print("  %-5s %s  %s" % (f["level"], loc, f["msg"]))
        if report["mounts"]:
            print("-" * 60)
            print("挂载覆盖（仓库名 %s）:" % repo.name)
            for m in report["mounts"]:
                tag = {"ok": "正常", "missing": "缺失", "foreign": "异常"}[m["status"]]
                print("  [%s] %s  %s" % (tag, m["root"], m["msg"]))
        print("=" * 60)
        print("结论: %d 个 ERROR, %d 个 WARN。%s" % (
            n_err, n_warn,
            "换机/提交前必须清掉 ERROR。" if n_err else "可移植性检查通过。"))
    return 1 if n_err else 0


if __name__ == "__main__":
    sys.exit(main())