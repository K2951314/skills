"""批量换机注册表：一份 TOML 清单，列出要一起换机的项目。

注册表本身不进任何项目仓库——它跨项目，属于「这台机器」的配置。
默认位置 ``~/.migrate-registry.toml``，可用 ``--registry`` 覆盖。

设计要点（踩过的坑，勿回退）：
- 路径支持 ``~``、``$HOME``、``%USERPROFILE%`` 展开，不硬编码盘符/用户名。
  注册表可以放 OneDrive 之类同步盘里跨设备携带，路径写 ``~/projects/foo``
  在任何机器上都对。
- 每个项目可带 ``profile`` 覆盖（如某项目只换电脑不带数据库）。
- 加载即校验：路径不存在、manifest 缺失、slug 不合法，立刻报清楚哪条错了。
  批量执行最怕的是「一条错的静默跳过，后面九条跑完才发现第一条没导」。
- 注册表只存「在哪」「用什么 profile」，不存口令、不存服务器地址。
  口令每次交互输入（或 ``--passphrase-file``），服务器地址每次 ``--target``。
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import EXIT_ERROR, EXIT_USAGE
from .manifest import _SLUG

#: 注册表默认位置。放用户目录而非项目目录：它跨项目，属于本机配置。
DEFAULT_REGISTRY_PATH = "~/.migrate-registry.toml"

#: 注册表 schema 版本。未来字段变更时升版，引擎据此拒绝过新的格式。
REGISTRY_SCHEMA_VERSION = 1


class RegistryError(Exception):
    """注册表缺失、损坏或不符合 schema。"""

    def __init__(self, message: str, *, exit_code: int = EXIT_USAGE):
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class RegistryEntry:
    """注册表里的一条项目记录。"""
    name: str               # 注册表里的别名（供人看、供 --only 过滤）
    path: str               # 原始路径串（展开前的样子，报错时给用户看）
    resolved: Path          # 展开后的绝对路径
    profile: str | None = None   # 可选：覆盖 manifest 的 profile 选择


@dataclass
class Registry:
    version: int
    entries: list[RegistryEntry] = field(default_factory=list)
    path: Path | None = None   # 加载来源（报错/汇报用）


def expand_path(raw: str) -> Path:
    """展开 ``~``、``$VAR``、``%VAR%`` 后返回 Path。

    刻意只做这三类展开，不调 ``Path.resolve()``：后者会跟随符号链接，
    把 junction 目标暴露给用户——注册表里写着 ``D:\\foo``，用户以为
    路径写死了，其实原始写的是 ``~/foo``。

    注意：``os.path.expandvars`` 在 POSIX 上展开 ``$VAR``，在 Windows 上
    只展开 ``%VAR%``。为了让注册表跨平台一致（写 ``$HOME`` 在任何 OS
    都对），这里补一层 ``$VAR`` / ``${VAR}`` 的手动展开。
    """
    s = raw.strip()
    # ``~`` 展开（os.path.expanduser 在 Windows 上也认 %USERPROFILE%）
    s = os.path.expanduser(s)
    # ``$VAR`` / ``${VAR}`` 展开：Windows 上 expandvars 不认这个，手动补
    s = _expand_dollar_vars(s)
    # ``%VAR%`` 展开（Windows 风格，expandvars 在 Windows 上认这个；
    # POSIX 上不认，手动补一层）
    s = os.path.expandvars(s)
    if "%" in s:
        s = _expand_win_vars(s)
    return Path(s)


def _expand_dollar_vars(s: str) -> str:
    """展开 ``$VAR`` 和 ``${VAR}`` 风格的环境变量（跨平台）。

    ``os.path.expandvars`` 在 Windows 上不展开 ``$VAR``，但注册表可能
    写 ``$HOME``（POSIX 习惯）——为了让注册表跨平台一致，这里手动展开。
    """
    pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")

    def repl(match: re.Match) -> str:
        name = match.group(1) or match.group(2)
        return os.environ.get(name, match.group(0))

    return pattern.sub(repl, s)


def _expand_win_vars(s: str) -> str:
    """二次展开 ``%USERPROFILE%`` 风格的 Windows 环境变量。"""
    pattern = re.compile(r"%([A-Za-z0-9_]+)%")

    def repl(match: re.Match) -> str:
        return os.environ.get(match.group(1), match.group(0))

    return pattern.sub(repl, s)


def _check_entry(raw: dict, index: int, problems: list[str]) -> tuple[str, str, str | None] | None:
    """校验一条 [[projects]] 记录。返回 (name, path, profile) 或 None。"""
    if not isinstance(raw, dict):
        problems.append(f"projects[{index}] 必须是表")
        return None

    name = raw.get("name")
    if not isinstance(name, str) or not _SLUG.match(name or ""):
        problems.append(
            f"projects[{index}].name 必须是小写字母/数字/连字符 slug"
            "（如 smart-quotation）"
        )
        name = f"project-{index}"

    path = raw.get("path")
    if not isinstance(path, str) or not path.strip():
        problems.append(f"[{name}] path 必须是非空字符串")
        return None

    profile = raw.get("profile")
    if profile is not None and (not isinstance(profile, str) or not profile.strip()):
        problems.append(f"[{name}] profile 必须是字符串或省略")
        profile = None

    # 未知字段报错：拼错的键静默忽略是批量执行最常见的坑
    known = {"name", "path", "profile"}
    for key in raw:
        if key not in known:
            problems.append(
                f"[{name}] 含未知字段 {key!r}（拼写错误？可用：{sorted(known)}）"
            )

    return name, path, profile


def validate_registry(data: dict) -> tuple[list[str], Registry]:
    """校验注册表数据。返回 (problems, registry)。problems 非空即不合法。"""
    problems: list[str] = []
    if not isinstance(data, dict):
        return (["注册表顶层必须是表（table）"], Registry(version=0))

    version = data.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        problems.append("schema_version 必须是整数")
        version = 0
    elif version > REGISTRY_SCHEMA_VERSION:
        raise RegistryError(
            f"注册表 schema_version={version} 高于引擎支持的 "
            f"{REGISTRY_SCHEMA_VERSION}，请更新 one-click-migrate 技能后再试。",
            exit_code=EXIT_ERROR,
        )
    elif version < 1:
        problems.append("schema_version 必须 >= 1")

    projects = data.get("projects")
    if not isinstance(projects, list) or not projects:
        problems.append("projects 必须是非空数组（[[projects]]）")
        projects = []

    entries: list[RegistryEntry] = []
    seen_names: set[str] = set()
    for i, raw in enumerate(projects):
        checked = _check_entry(raw, i, problems)
        if checked is None:
            continue
        name, path_raw, profile = checked
        if name in seen_names:
            problems.append(f"[{name}] name 重复")
            continue
        seen_names.add(name)
        entries.append(RegistryEntry(
            name=name, path=path_raw, resolved=expand_path(path_raw),
            profile=profile,
        ))

    known_top = {"schema_version", "projects"}
    for key in data:
        if key not in known_top:
            problems.append(f"未知顶层字段：{key!r}（拼写错误？引擎按忽略处理，但请确认）")

    return problems, Registry(version=version, entries=entries)


def find_registry_path(explicit: str | None = None) -> Path:
    """返回注册表路径。explicit 优先，否则用默认 ``~/.migrate-registry.toml``。"""
    if explicit:
        return expand_path(explicit)
    return expand_path(DEFAULT_REGISTRY_PATH)


def load_registry(path: Path | str | None = None) -> Registry:
    """加载并校验注册表。失败抛 RegistryError。

    path=None 时用默认位置。文件不存在不报错——返回空注册表，
    让调用方提示用户先建一个（给出模板比报错有用）。
    """
    if path is None:
        resolved_path = find_registry_path(None)
    else:
        resolved_path = expand_path(str(path)) if isinstance(path, str) else path

    if not resolved_path.is_file():
        raise RegistryError(
            f"没找到注册表：{resolved_path}\n"
            "批量换机需要一份注册表，列出要一起换机的项目。"
            "先用 `migrate batch init` 生成草稿，或手动建一份，示例：\n\n"
            '  schema_version = 1\n'
            '  [[projects]]\n'
            '  name = "my-project"\n'
            '  path = "~/projects/my-project"\n'
            "  # profile = \"machine-only\"  # 可选：覆盖 manifest 的 profile\n"
        )

    try:
        data = tomllib.loads(resolved_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise RegistryError(f"注册表不是合法 TOML：{exc}") from exc
    except UnicodeDecodeError as exc:
        raise RegistryError(f"注册表必须是 UTF-8 编码：{exc}") from exc

    try:
        problems, registry = validate_registry(data)
    except RegistryError:
        raise
    if problems:
        raise RegistryError(
            "注册表不符合 schema：\n" + "\n".join(f"  - {p}" for p in problems)
        )
    registry.path = resolved_path
    return registry


def filter_entries(registry: Registry, only: list[str] | None,
                   exclude: list[str] | None) -> list[RegistryEntry]:
    """按 --only / --exclude 过滤注册表条目。

    --only 只跑列出的 name；--exclude 跳过列出的 name。两者可混用
    （先 only 再 exclude）。未匹配的 name 报错——拼错项目名时静默跳过
    是批量执行最隐蔽的失败。
    """
    entries = list(registry.entries)
    if only:
        only_set = set(only)
        unknown = only_set - {e.name for e in entries}
        if unknown:
            raise RegistryError(
                f"--only 引用了注册表里没有的项目：{sorted(unknown)}。"
                f"注册表里有：{sorted(e.name for e in entries)}"
            )
        entries = [e for e in entries if e.name in only_set]
    if exclude:
        exc_set = set(exclude)
        unknown = exc_set - {e.name for e in registry.entries}
        if unknown:
            raise RegistryError(
                f"--exclude 引用了注册表里没有的项目：{sorted(unknown)}。"
                f"注册表里有：{sorted(e.name for e in registry.entries)}"
            )
        entries = [e for e in entries if e.name not in exc_set]
    return entries


DRAFT_TEMPLATE = '''# 一键换机注册表 —— 列出要一起换机的项目。
# 生成命令：migrate batch init
#
# 这个文件不进任何项目仓库——它跨项目，属于「这台机器」的配置。
# 默认位置 ~/.migrate-registry.toml，可以放同步盘（OneDrive 等）跨设备携带。
#
# 路径写法（跨设备兼容）：
#   ~/projects/foo          ~ 在 Windows/macOS/Linux 都展开
#   $HOME/projects/foo      POSIX 风格环境变量
#   %USERPROFILE%/projects/foo   Windows 风格（注册表放同步盘时推荐）
#   绝对路径 D:/foo 也能用，但换设备就对不上了——除非确定只在当前设备跑
#
# name：注册表里的别名，供 --only / --exclude 过滤用，必须是小写 slug
# profile：可选，覆盖 manifest 里的 --profile（如 machine-only 只换电脑不带数据库）

schema_version = 1

[[projects]]
name = "smart-quotation"
path = "~/projects/smart-quotation"
# profile = "machine-only"

[[projects]]
name = "zk-ai"
path = "~/AI/ZK-AI"

[[projects]]
name = "dianping"
path = "~/大众点评"
'''


def scaffold_registry() -> str:
    """生成注册表草稿文本。"""
    return DRAFT_TEMPLATE
