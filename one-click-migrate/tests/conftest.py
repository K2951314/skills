"""测试公共夹具。

- SRC：把引擎目录加进 sys.path（引擎是标准库-only 的包）。
- fake_project：一个带 .gitignore、密钥、SQLite、本地配置的假项目。
- secret_values：假密钥值清单，用于脱敏断言（输出里不得出现它们）。
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FAKE_JWT = "super-secret-jwt-value-do-not-leak-123456"
FAKE_ADMIN_KEY = "admin-api-key-abcdef987654"


@pytest.fixture()
def fake_project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / ".migrate").mkdir(parents=True)
    (root / ".env").write_text(
        f"JWT_SECRET={FAKE_JWT}\nADMIN_API_KEY={FAKE_ADMIN_KEY}\nSQ_DEV=1\n",
        encoding="utf-8",
    )
    (root / "config.local.json").write_text('{"tier": "pro"}', encoding="utf-8")
    (root / "_ops-notes.md").write_text("# 运维笔记\n服务器细节写在项目文档，不写这里。\n", encoding="utf-8")
    db = root / "data"
    db.mkdir()
    conn = sqlite3.connect(db / "app.db")
    conn.execute("create table if not exists t (id integer primary key, v text)")
    conn.execute("insert into t (v) values ('x')")
    conn.commit()
    conn.close()
    # 明确的垃圾：不该出现在建议区
    (root / "node_modules").mkdir()
    (root / "node_modules" / "leftpad.js").write_text("//", encoding="utf-8")
    (root / ".gitignore").write_text(
        ".env\nconfig.local.json\n_*.md\nnode_modules/\ndata/\n.env.*\n!logs\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture()
def git_project(fake_project: Path, tmp_path: Path) -> Path:
    """fake_project + git init（无 git 时跳过）。"""
    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        pytest.skip("git 不可用")
    subprocess.run(["git", "init", "-q"], cwd=fake_project, check=True,
                   capture_output=True)
    subprocess.run(["git", "add", ".gitignore"], cwd=fake_project, check=True,
                   capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"],
        cwd=fake_project, check=True, capture_output=True,
    )
    return fake_project


@pytest.fixture()
def secret_values() -> list[str]:
    return [FAKE_JWT, FAKE_ADMIN_KEY]


MANIFEST_TEXT = """
schema_version = 1
project = "fake-proj"
artifacts_dir = ".migrate"

rebuild = ["python -m venv .venv"]
verify = ["python -m pytest tests/ -q"]

[[items]]
id = "env-file"
path = ".env"
class = "required"
on_missing = "block"
sensitive = true

[[items]]
id = "local-config"
path = "config.local.json"
class = "recommended"

[[items]]
id = "app-db"
path = "data/app.db"
item_type = "sqlite"
class = "recommended"

[[items]]
id = "ops-notes"
path = "_ops-notes.md"
class = "recommended"

[profiles.machine-only]
exclude = ["app-db"]

[merge]
include = [".env", "config.local.json"]

[env_audit]
code_dirs = ["src"]
tooling_only = ["SQ_DEV"]
server_only = ["SMTP_HOST"]
"""


@pytest.fixture()
def project_with_manifest(fake_project: Path) -> Path:
    (fake_project / ".migrate" / "manifest.toml").write_text(MANIFEST_TEXT, encoding="utf-8")
    return fake_project
