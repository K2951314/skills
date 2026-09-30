"""环境变量键名审计：只读键名，绝不读值、绝不打印值。

通用化自智能询价 scripts/env_audit.py（该文件是库、无 CLI，且分类集合
硬编码在代码里）。这里把分类策略外置到 manifest 的 [env_audit]，引擎只提供
扫描框架。

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

#: env 文件里的键名行。只取名字，不取值。
_ENV_LINE = re.compile(r"""^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=""")

#: 扫描时跳过的目录。
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".migrate",
              "_换机", "dist", "build", "out", "coverage", ".pytest_cache"}


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
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            scanned += 1
            for pattern in (_ENV_GET, _ENV_IDX, _ENV_GETENV, _ENV_NAME):
                keys.update(pattern.findall(text))
    return keys if scanned else set()


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

    result = AuditResult()
    result.code_keys = scan_code_keys(root, code_dirs)
    if not result.code_keys:
        result.scan_failed = True  # 什么都没扫到：不报「正常」，报「没扫到」

    for rel in (env_files or [".env"]):
        result.env_keys |= read_env_file_keys(root / rel)
    result.env_keys |= non_python  # 非 Python 消费者的键视为「已配置」

    # 旧名映射：代码扫到旧名时，只要新名配了就算配过
    effective_code = set(result.code_keys)
    for old, new in deprecated.items():
        if old in result.code_keys:
            effective_code.discard(old)
            effective_code.add(new)

    ignore = tooling_only | defaulted
    result.missing = effective_code - result.env_keys - ignore
    result.missing_required = result.missing & prod_required
    result.dead = result.env_keys - effective_code - ignore
    # server_only 是「只应在部署机上」的键：本机没有不是缺失，但要提醒
    result.server_only = server_only_declared & result.env_keys
    return result
