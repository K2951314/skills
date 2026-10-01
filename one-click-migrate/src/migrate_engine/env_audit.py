"""环境变量键名审计：只读键名，绝不读值、绝不打印值。

通用化自智能询价 scripts/env_audit.py 与 ZK-AI scripts/migrate.py 的
_credential_env_refs。这里把分类策略外置到 manifest 的 [env_audit]，
引擎只提供扫描框架。

两类消费者，缺一不可：
1. **Python 源码**里的 os.environ / os.getenv（通用）；
2. **声明式扫描**（[[env_audit.consumers]]）：凭据名来自 YAML/JSON/shell 的
   非 Python 配置。ZK-AI 的真实教训——providers.yaml 的 `env: SENSENOVA_API_KEY`
   与 burner.yaml 的 only: 列表驱动十几个凭据，Python 静态扫描完全看不到。
   不扫这类消费者，那些键会被判成「死键，确认后可删」，照做网关当场全瘫
   （2026-09-29 SENSENOVA_API_KEY 静默消失的同一类问题）。

铁律：输出只有键名。.env 里出现任何值都不进报告——审计的是「配没配」，
不是「配了什么」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

#: 代码里读环境变量的常见写法。
_ENV_GET = re.compile(r"""os\.environ\.get\(\s*["']([A-Z][A-Z0-9_]*)["']""")
_ENV_IDX = re.compile(r"""os\.environ\[\s*["']([A-Z][A-Z0-9_]*)["']\s*\]""")
_ENV_GETENV = re.compile(r"""os\.getenv\(\s*["']([A-Z][A-Z0-9_]*)["']""")
_ENV_NAME = re.compile(r"""environ\.get\(\s*["']([A-Z][A-Z0-9_]*)["']""")

# 注意：不要用「全大写标识符」当兜底去扫框架式读取（pydantic-settings /
# 依赖注入）。实测过：它会把 109 个普通常量（HTTP_400_BAD_REQUEST、
# QUARANTINE_LADDER、ROLE_HINTS…）当成 env 键名，missing 从 3 条涨到 109 条，
# 真正的信号被噪音埋掉。要覆盖框架式读取，用 [[env_audit.consumers]] 显式声明。

#: env 文件里的键名行。只取名字，不取值。
_ENV_LINE = re.compile(r"""^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=""")

#: 扫描时跳过的目录。
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".migrate",
              "_换机", "dist", "build", "out", "coverage", ".pytest_cache"}

#: 本模块自身、以及所有测试/文档里的示例代码：它们的 docstring 写着
#: `os.environ.get("X")` 这类示意代码，正则会把示例里的名字当成真实读取。
#: 智能询价的 env_audit.py 用一个 SELF_EXCLUDE 集合处理同一件事，且注释里
#: 记着「实际发生过，报出一个幽灵键 X」。这里按文件名统一排除。
_SELF_FILES = frozenset({"env_audit.py", "env_health.py", "migrate.py"})


@dataclass
class AuditResult:
    code_keys: set[str] = field(default_factory=set)
    env_keys: set[str] = field(default_factory=set)
    missing: set[str] = field(default_factory=set)      # 代码读了，哪里都没配
    missing_required: set[str] = field(default_factory=set)  # 其中生产必需的（红）
    dead: set[str] = field(default_factory=set)         # 配了，没代码读
    server_only: set[str] = field(default_factory=set)  # 只应在部署机上的键
    scan_failed: bool = False                           # fail-closed：扫不到代码不报「正常」
    files_scanned: int = 0


def scan_code_keys(root: Path, code_dirs: list[str]) -> set[str]:
    """在 code_dirs 里扫 Python 源码引用的环境变量名。"""
    keys: set[str] = set()
    scanned = 0
    for rel in code_dirs or ["."]:
        base = root / rel
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            if path.name in _SELF_FILES:
                # 自己的 docstring/注释里有示意代码，会把示例键名当真实读取
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            scanned += 1
            for pattern in (_ENV_GET, _ENV_IDX, _ENV_GETENV, _ENV_NAME):
                keys.update(pattern.findall(text))
    return keys if scanned else set()


def scan_declared_consumers(root: Path, consumers: list[dict]) -> set[str]:
    """按 manifest 的 [[env_audit.consumers]] 扫非 Python 配置里的键名。

    每个 consumer 声明：
      glob     —— 要扫的文件（相对 root，支持 * ?）
      pattern  —— 正则；第一个捕获组是键名（允许有两个组，取非空的那个）
      section  —— 可选：只扫该标题之下的块（如 burner.yaml 的 only:）
      split    —— 可选：对捕获到的值再按这些字符切开。
                   ZK-AI 的 `only: "A,B,C"` 是逗号连接的单行而不是列表，
                   pattern 抓到的是一整串，不切分就一个键都认不出来。

    刻意用「声明式正则」而不是按格式解析（YAML/JSON/TOML 各写一个）：
    配置坏掉时扫描仍要能工作——配置坏了恰恰是操作者最需要备份的时刻，
    解析会把响亮的失败换成沉默的失败。代价是只认声明里写明的两种形态，
    它是个告警工具，不需要对全部内容正确。
    """
    keys: set[str] = set()
    for consumer in consumers or []:
        if not isinstance(consumer, dict):
            continue
        pattern_text = consumer.get("pattern")
        glob_text = consumer.get("glob")
        if not pattern_text or not glob_text:
            continue
        section = consumer.get("section")
        split_chars = consumer.get("split") or ""
        try:
            rx = re.compile(pattern_text)
        except re.error:
            continue
        import fnmatch

        for path in sorted(root.glob(glob_text)):
            if not path.is_file():
                continue
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            in_section = section is None
            for line in text.splitlines():
                if section is not None:
                    if line.strip().startswith(f"{section}:"):
                        in_section = True
                        continue
                    if in_section and line.strip() and not line.lstrip().startswith(("#", "-")):
                        in_section = False
                if not in_section:
                    continue
                for match in rx.finditer(line):
                    name = next((g for g in match.groups() if g), None)
                    if not name:
                        continue
                    if split_chars:
                        for part in re.split(f"[{re.escape(split_chars)}]", name):
                            part = part.strip()
                            if part:
                                keys.add(part)
                    else:
                        keys.add(name)
    return keys


def read_env_file_keys(path: Path) -> set[str]:
    """只取键名。值永远不进返回值。"""
    keys: set[str] = set()
    if not path.is_file():
        return keys
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return keys
    for line in text.splitlines():
        match = _ENV_LINE.match(line)
        if match:
            keys.add(match.group(1))
    return keys


def audit(root: Path, env_audit_policy: dict, *, env_files: list[str] | None = None) -> AuditResult:
    """按 manifest 的 [env_audit] 策略做一次键名审计。"""
    policy = env_audit_policy or {}
    code_dirs = policy.get("code_dirs", [])
    tooling_only = set(policy.get("tooling_only", []))
    defaulted = set(policy.get("defaulted_or_optional", []))
    deprecated = dict(policy.get("deprecated_aliases", {}))
    non_python = set(policy.get("non_python_consumers", []))
    server_only_declared = set(policy.get("server_only", []))
    prod_required = set(policy.get("prod_required", []))
    consumers = policy.get("consumers") or []

    result = AuditResult()
    result.code_keys = scan_code_keys(root, code_dirs)
    py_scan_empty = not result.code_keys
    # 声明式消费者与 Python 静态扫描同等对待：它们都是「有代码在读这个键」
    result.code_keys |= scan_declared_consumers(root, consumers)
    for rel in (env_files or [".env"]):
        result.env_keys |= read_env_file_keys(root / rel)
    result.env_keys |= non_python  # 非 Python 消费者的键视为「已配置」
    result.server_only = server_only_declared & result.env_keys

    if not result.code_keys and py_scan_empty:
        # 什么都没扫到：报「没扫到」，且**绝不能报 missing / dead**。
        # 扫不到时代码侧是空集，`dead = env_keys - 空集` 会把每一个已配键都说成
        # 「没有代码读，确认后可删」——那是诱导用户删正在用的配置。
        # ZK-AI 实测一次性误报 48 个凭据键（含 ZKAI_DATABASE_URL、ZKAI_ADMIN_TOKEN）。
        # 一个在自己失效时反而给出危险建议的检查，比没有检查更糟。
        result.scan_failed = True
        return result

    # 旧名映射：代码扫到旧名时，只要新名配了就算配过
    effective_code = set(result.code_keys) | non_python
    for old, new in deprecated.items():
        if old in result.code_keys:
            effective_code.discard(old)
            effective_code.add(new)

    ignore = tooling_only | defaulted
    result.missing = effective_code - result.env_keys - ignore
    result.missing_required = result.missing & prod_required
    result.dead = result.env_keys - effective_code - ignore
    return result
