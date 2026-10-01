"""项目声明 manifest：.migrate/manifest.toml 的加载、校验、profile 选择。

用 TOML 而不是 YAML：Python 3.11+ 自带 tomllib，解析行为由标准库保证，
不必在手写 YAML 子集解析器里养 bug。注释能力保留（运维语义写在这里）。

分离原则：
- 项目 manifest 声明「该搬什么」——静态、可提交、不含密钥值。
- 包内 manifest.json 记录「这一包装了什么」——生成物，带逐文件 sha256。
- 运行快照（exists/bytes/mtime）绝不写回项目 manifest，避免每次运行制造 diff。
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import EXIT_UNSUPPORTED, MIN_PYTHON

MANIFEST_REL = ".migrate/manifest.toml"
SCHEMA_VERSION = 1

#: 顶层默认产物目录。项目可在 manifest 里覆盖（智能询价沿用 _换机）。
DEFAULT_ARTIFACTS_DIR = ".migrate"

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_DRIVE = re.compile(r"^[A-Za-z]:")


class ManifestError(Exception):
    """manifest 缺失、损坏或不符合 schema。"""

    def __init__(self, message: str, *, exit_code: int = 2):
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class Item:
    id: str
    path: str
    cls: str = "required"
    item_type: str = "file"
    scope: str = "workspace"
    on_missing: str = "warn"
    sensitive: bool = False
    review_status: str = "confirmed"
    note: str = ""
    # server 作用域专用
    remote_path: str | None = None
    capture: str | None = None
    pg_user: str | None = None
    pg_db: str | None = None


@dataclass
class Manifest:
    project: str
    items: list[Item] = field(default_factory=list)
    artifacts_dir: str = DEFAULT_ARTIFACTS_DIR
    rebuild: list[str] = field(default_factory=list)
    verify: list[str] = field(default_factory=list)
    merge_include: list[str] = field(default_factory=list)
    env_audit: dict = field(default_factory=dict)
    profiles: dict[str, dict] = field(default_factory=dict)
    root: Path | None = None
    raw: dict = field(default_factory=dict)


# ── 校验（与 schemas/manifest.v1.schema.json 对齐，测试钉死一致性） ───────

_CLASSES = {"required", "recommended", "archive"}
_ITEM_TYPES = {"file", "dir", "glob", "sqlite", "pg"}
_SCOPES = {"workspace", "server", "both"}
_ON_MISSING = {"block", "warn", "skip"}
_REVIEW = {"confirmed", "needs-review", "skipped_by_user"}
_CAPTURES = {"file", "pg_dump", "command"}


def _check_path(item_id: str, path: object, problems: list[str]) -> str:
    if not isinstance(path, str) or not path.strip():
        problems.append(f"[{item_id}] path 必须是非空字符串")
        return ""
    norm = path.replace("\\", "/").strip()
    if norm.startswith("/") or _DRIVE.match(norm) or norm.startswith("//"):
        problems.append(f"[{item_id}] path 必须是项目内相对路径：{path!r}")
    if ".." in norm.split("/"):
        problems.append(f"[{item_id}] path 不允许出现 ..：{path!r}")
    if any(part == "" for part in norm.split("/")[:-1]):
        problems.append(f"[{item_id}] path 含空路径段：{path!r}")
    return norm


def validate_data(data: dict) -> tuple[list[str], Manifest]:
    """校验并构造 Manifest。返回 (problems, manifest)；problems 非空即不合法。"""
    problems: list[str] = []
    if not isinstance(data, dict):
        return (["manifest 顶层必须是表（table）"], Manifest(project=""))

    version = data.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        problems.append("schema_version 必须是整数")
    elif version > SCHEMA_VERSION:
        raise ManifestError(
            f"manifest schema_version={version} 高于引擎支持的 {SCHEMA_VERSION}，"
            "请更新 one-click-migrate 技能后再试。",
            exit_code=EXIT_UNSUPPORTED,
        )
    elif version < 1:
        problems.append("schema_version 必须 >= 1")

    project = data.get("project")
    if not isinstance(project, str) or not _SLUG.match(project or ""):
        problems.append("project 必须是小写字母/数字/连字符 slug（如 smart-quotation）")

    artifacts_dir = data.get("artifacts_dir", DEFAULT_ARTIFACTS_DIR)
    if not isinstance(artifacts_dir, str) or not artifacts_dir.strip():
        problems.append("artifacts_dir 必须是非空字符串")
        artifacts_dir = DEFAULT_ARTIFACTS_DIR
    artifacts_dir = artifacts_dir.replace("\\", "/").strip().strip("/")

    items_data = data.get("items")
    if not isinstance(items_data, list) or not items_data:
        problems.append("items 必须是非空数组（[[items]]）")
        items_data = []

    items: list[Item] = []
    seen_ids: set[str] = set()
    item_fields = {"id", "path", "class", "item_type", "scope", "on_missing",
                   "sensitive", "review_status", "note", "remote_path", "capture",
                   "pg_user", "pg_db"}
    for i, raw in enumerate(items_data):
        if not isinstance(raw, dict):
            problems.append(f"items[{i}] 必须是表")
            continue
        for key in raw:
            if key not in item_fields:
                problems.append(
                    f"items[{i}] 含未知字段 {key!r}（TOML 作用域陷阱：[[items]] 之后的键"
                    "会落进该项，rebuild/verify 等顶层字段要放在第一个 [[items]] 之前）"
                )
        item_id = raw.get("id")
        if not isinstance(item_id, str) or not _SLUG.match(item_id or ""):
            problems.append(f"items[{i}].id 必须是小写字母/数字/连字符 slug")
            item_id = f"item-{i}"
        if item_id in seen_ids:
            problems.append(f"items[{i}].id 重复：{item_id}")
        seen_ids.add(item_id)

        path = _check_path(item_id, raw.get("path"), problems)
        cls = raw.get("class", "required")
        if cls not in _CLASSES:
            problems.append(f"[{item_id}] class 必须是 {sorted(_CLASSES)} 之一，当前 {cls!r}")
        item_type = raw.get("item_type", "file")
        if item_type not in _ITEM_TYPES:
            problems.append(f"[{item_id}] item_type 必须是 {sorted(_ITEM_TYPES)} 之一，当前 {item_type!r}")
        scope = raw.get("scope", "workspace")
        if scope not in _SCOPES:
            problems.append(f"[{item_id}] scope 必须是 {sorted(_SCOPES)} 之一，当前 {scope!r}")
        on_missing = raw.get("on_missing", "warn")
        if on_missing not in _ON_MISSING:
            problems.append(f"[{item_id}] on_missing 必须是 {sorted(_ON_MISSING)} 之一，当前 {on_missing!r}")
        review = raw.get("review_status", "confirmed")
        if review not in _REVIEW:
            problems.append(f"[{item_id}] review_status 必须是 {sorted(_REVIEW)} 之一，当前 {review!r}")

        remote_path = raw.get("remote_path")
        capture_for_check = raw.get("capture")
        if (scope in {"server", "both"} and not remote_path
                and capture_for_check != "pg_dump"):
            problems.append(f"[{item_id}] scope={scope} 必须提供 remote_path"
                            "（capture=pg_dump 用 pg_db 代替）")
        if remote_path is not None and (not isinstance(remote_path, str) or not remote_path.strip()):
            problems.append(f"[{item_id}] remote_path 必须是非空字符串")
            remote_path = None

        capture = raw.get("capture")
        if capture is not None and capture not in _CAPTURES:
            problems.append(f"[{item_id}] capture 必须是 {sorted(_CAPTURES)} 之一，当前 {capture!r}")

        pg_user = raw.get("pg_user")
        pg_db = raw.get("pg_db")
        if capture == "pg_dump" and not pg_db:
            problems.append(f"[{item_id}] capture=pg_dump 必须提供 pg_db（库名）")
        for fname, fvalue in (("pg_user", pg_user), ("pg_db", pg_db)):
            if fvalue is not None and (not isinstance(fvalue, str) or not fvalue.strip()):
                problems.append(f"[{item_id}] {fname} 必须是非空字符串")
        if capture == "pg_dump":
            pg_user = pg_user or "postgres"

        sensitive = raw.get("sensitive", False)
        if not isinstance(sensitive, bool):
            problems.append(f"[{item_id}] sensitive 必须是布尔值")
            sensitive = False
        note = raw.get("note", "")
        if not isinstance(note, str):
            problems.append(f"[{item_id}] note 必须是字符串")
            note = ""

        items.append(Item(
            id=item_id, path=path, cls=cls, item_type=item_type, scope=scope,
            on_missing=on_missing, sensitive=sensitive, review_status=review,
            note=note, remote_path=remote_path, capture=capture,
            pg_user=pg_user, pg_db=pg_db,
        ))

    profiles = data.get("profiles", {})
    if not isinstance(profiles, dict):
        problems.append("profiles 必须是表（[profiles.<名字>]）")
        profiles = {}
    known_ids = seen_ids | {"*"}
    for pname, prof in profiles.items():
        if not isinstance(prof, dict):
            problems.append(f"profiles.{pname} 必须是表")
            continue
        for key in ("include", "exclude"):
            values = prof.get(key, [])
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                problems.append(f"profiles.{pname}.{key} 必须是字符串数组")
                continue
            for v in values:
                if v not in known_ids:
                    problems.append(f"profiles.{pname}.{key} 引用了未知 id：{v!r}")

    merge = data.get("merge", {})
    merge_include = merge.get("include", []) if isinstance(merge, dict) else []
    if not isinstance(merge_include, list) or not all(isinstance(v, str) for v in merge_include):
        problems.append("merge.include 必须是字符串数组")
        merge_include = []

    env_audit = data.get("env_audit", {})
    if not isinstance(env_audit, dict):
        problems.append("env_audit 必须是表")
        env_audit = {}
    else:
        # 嵌套表键名白名单：拼错必须报错，不能静默失效。
        # 真实的坑：`merge.includes`（多一个 s）让导入白名单整个消失，
        # 而 whitelist=None 在导入侧表示「无限制」——限制被悄悄撤掉了。
        known_env_audit = {
            "code_dirs", "tooling_only", "defaulted_or_optional", "deprecated_aliases",
            "non_python_consumers", "prod_required", "server_only", "env_files",
            "consumers",
        }
        for key in env_audit:
            if key not in known_env_audit:
                problems.append(
                    f"env_audit 含未知字段 {key!r}（拼写错误？已忽略。"
                    f"可用：{sorted(known_env_audit)}）"
                )
        efiles = env_audit.get("env_files")
        if efiles is not None and (
                not isinstance(efiles, list)
                or not all(isinstance(v, str) and v.strip() for v in efiles)):
            problems.append("env_audit.env_files 必须是非空字符串数组（如 [\".env\", \".env.server\"]）")
        consumers = env_audit.get("consumers")
        if consumers is not None:
            if not isinstance(consumers, list):
                problems.append("env_audit.consumers 必须是表数组（[[env_audit.consumers]]）")
            else:
                for i, c in enumerate(consumers):
                    if not isinstance(c, dict):
                        problems.append(f"env_audit.consumers[{i}] 必须是表")
                        continue
                    for fname in ("glob", "pattern"):
                        if not isinstance(c.get(fname), str) or not c.get(fname, "").strip():
                            problems.append(
                                f"env_audit.consumers[{i}] 必须提供 {fname}（非空字符串）")
                    if c.get("pattern"):
                        try:
                            re.compile(c["pattern"])
                        except re.error as exc:
                            problems.append(
                                f"env_audit.consumers[{i}].pattern 不是合法正则：{exc}")
        known_merge = {"include"}
        merge_for_check = data.get("merge", {})
        if isinstance(merge_for_check, dict):
            for key in merge_for_check:
                if key not in known_merge:
                    problems.append(
                        f"merge 含未知字段 {key!r}（拼写错误？已忽略。"
                        f"可用：{sorted(known_merge)}）"
                    )

    for key in ("rebuild", "verify"):
        values = data.get(key, [])
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            problems.append(f"{key} 必须是字符串数组")
            values = []
        if key == "rebuild":
            rebuild = values
        else:
            verify = values

    known_top = {
        "schema_version", "project", "artifacts_dir", "items", "profiles",
        "env_audit", "merge", "rebuild", "verify",
    }
    for key in data:
        if key not in known_top:
            problems.append(f"未知顶层字段：{key!r}（拼写错误？引擎按忽略处理，但请确认）")

    manifest = Manifest(
        project=project or "",
        items=items,
        artifacts_dir=artifacts_dir,
        rebuild=rebuild,
        verify=verify,
        merge_include=merge_include,
        env_audit=env_audit,
        profiles=profiles,
        raw=data,
    )
    return problems, manifest


# ── 加载 ────────────────────────────────────────────────────────────────


def find_manifest(root: Path) -> Path | None:
    candidate = root / MANIFEST_REL
    return candidate if candidate.is_file() else None


def load_manifest(root: Path) -> Manifest:
    """加载并校验 <root>/.migrate/manifest.toml。失败抛 ManifestError。"""
    path = find_manifest(root)
    if path is None:
        raise ManifestError(
            f"没找到 {MANIFEST_REL}。\n"
            "先跑 `migrate manifest init` 生成草稿并确认，或 `migrate plan` 看扫描建议。"
        )
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(f"{MANIFEST_REL} 不是合法 TOML：{exc}") from exc
    except UnicodeDecodeError as exc:
        raise ManifestError(f"{MANIFEST_REL} 必须是 UTF-8 编码：{exc}") from exc
    problems, manifest = validate_data(data)
    if problems:
        raise ManifestError(
            f"{MANIFEST_REL} 不符合 schema：\n" + "\n".join(f"  - {p}" for p in problems)
        )
    manifest.root = root
    return manifest


# ── profile 选择 ────────────────────────────────────────────────────────


def select_items(manifest: Manifest, profile: str | None) -> list[Item]:
    """按 profile 过滤 items。无 profile 返回全部；未知 profile 报错。"""
    if profile is None:
        return list(manifest.items)
    prof = manifest.profiles.get(profile)
    if prof is None:
        available = ", ".join(sorted(manifest.profiles)) or "（无）"
        raise ManifestError(f"未知 profile：{profile!r}。可用：{available}")
    include = set(prof.get("include", ["*"]))
    exclude = set(prof.get("exclude", []))
    chosen: list[Item] = []
    for item in manifest.items:
        if "*" not in include and item.id not in include:
            continue
        if item.id in exclude:
            continue
        chosen.append(item)
    return chosen


# ── 草稿模板 ────────────────────────────────────────────────────────────

DRAFT_TEMPLATE = '''# 一键换机项目声明 —— 只描述「该搬什么」，不放任何密钥值。
# 生成命令：migrate manifest init --project <slug>
#
# TOML 作用域提醒：rebuild/verify 等顶层键必须放在第一个 [[items]] 之前，
# 否则会被收进最后一个条目（引擎会报「未知字段」，见 schemas/manifest.v1.schema.json）。
schema_version = 1
project = "{project}"
artifacts_dir = ".migrate"          # 打包产物目录（.gitignore 已排除）

# ── 打包后/导入后要做的重建与验证命令（记录进包内清单，不自动执行） ────
rebuild = [
  "python -m venv .venv",
  "pip install -r requirements.txt",
]
verify = [
  "python -m pytest tests/ -q",
]

# ── 条目：被 git 排除、但运行/部署又离不开的东西 ──────────────────────
# class:        required（缺了起不来，默认阻断导出）/ recommended（缺了能跑但丢数据）/ archive
# item_type:    file / dir / glob / sqlite（backup API 一致性快照，sidecar 不发货）
# scope:        workspace（本机）/ server（部署机）/ both
# on_missing:   block / warn / skip
# sensitive:    true 时汇报只出键名与体积
# review_status: confirmed / needs-review / skipped_by_user

[[items]]
id = "env-file"
path = ".env"
class = "required"
item_type = "file"
scope = "workspace"
on_missing = "block"
sensitive = true
note = "本地/部署环境变量"

[[items]]
id = "local-config"
path = "config.local.json"
class = "recommended"
note = "本地覆盖配置（若存在）"

# ── workspace 包导入时允许合并进项目根的路径白名单 ────────────────────
[merge]
include = [".env", "config.local.json"]

# ── 环境变量审计策略（键名集合；值永不入库） ──────────────────────────
[env_audit]
code_dirs = []                       # 扫描 environ.get("KEY") 的目录
tooling_only = []                    # 只在开发/CI 用，不算缺失
defaulted_or_optional = []           # 有默认值或可选功能，不算缺失
deprecated_aliases = {{}}             # 旧名 -> 新名
server_only = []                     # 只存在于部署机的键（换电脑包不带，需提醒）
'''


def scaffold_manifest(project: str) -> str:
    if not _SLUG.match(project or ""):
        raise ManifestError("project 必须是小写字母/数字/连字符 slug")
    return DRAFT_TEMPLATE.format(project=project)


def python_ok() -> bool:
    import sys

    return sys.version_info >= MIN_PYTHON
