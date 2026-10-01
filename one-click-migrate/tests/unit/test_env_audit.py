"""环境变量键名审计测试：只出键名，绝不出值。"""

from __future__ import annotations

from pathlib import Path

import pytest

from migrate_engine.env_audit import audit, read_env_file_keys, scan_code_keys


@pytest.fixture()
def code_project(tmp_path: Path) -> Path:
    root = tmp_path / "codeproj"
    src = root / "app"
    src.mkdir(parents=True)
    (src / "a.py").write_text(
        "import os\n"
        "A = os.environ.get('ADMIN_API_KEY')\n"
        "B = os.environ['JWT_SECRET']\n"
        "C = os.getenv('SMTP_HOST')\n"
        "D = os.environ.get('MISSING_KEY')\n",
        encoding="utf-8",
    )
    (root / ".env").write_text(
        "ADMIN_API_KEY=secret-value-1\n"
        "JWT_SECRET=secret-value-2\n"
        "SMTP_HOST=smtp.example.com\n"
        "DEAD_KEY=nobody-reads-me\n",
        encoding="utf-8",
    )
    return root


def test_scan_and_audit(code_project, secret_values):
    result = audit(code_project, {"code_dirs": ["app"]})
    assert result.code_keys == {"ADMIN_API_KEY", "JWT_SECRET", "SMTP_HOST", "MISSING_KEY"}
    assert result.missing == {"MISSING_KEY"}
    assert result.dead == {"DEAD_KEY"}
    assert not result.scan_failed


def test_policy_sets_quiet_the_noise(code_project):
    result = audit(code_project, {
        "code_dirs": ["app"],
        "tooling_only": ["MISSING_KEY"],
        "defaulted_or_optional": ["DEAD_KEY"],
    })
    assert result.missing == set()
    assert result.dead == set()


def test_non_python_consumers_are_not_dead_keys(tmp_path):
    """non_python_consumers 里的键不是死键（它们由非 Python 工具读）。

    智能询价的 SQ_PYTHON 由 scripts/selfhost.ps1 读——Python 静态扫描看不到，
    于是它配了也被判成「死键，确认后可删」。用户照做会让自托管脚本失效。
    """
    root = tmp_path / "proj"
    (root / "app").mkdir(parents=True)
    (root / "app" / "a.py").write_text("import os\nx=os.environ.get('REAL')\n", encoding="utf-8")
    (root / ".env").write_text("REAL=1\nSQ_PYTHON=D:/py\n", encoding="utf-8")
    result = audit(root, {"code_dirs": ["app"], "non_python_consumers": ["SQ_PYTHON"]})
    assert result.dead == set(), f"SQ_PYTHON 被误判为死键：{result.dead}"
    assert "SQ_PYTHON" in result.env_keys


def test_self_files_do_not_create_ghost_keys(tmp_path):
    """扫描要排除自身与示例代码——docstring 里的 `os.environ.get("X")` 不是真实读取。

    智能询价的 env_audit.py 用 SELF_EXCLUDE 处理同一件事，注释里记着
    「实际发生过，报出一个幽灵键 X」。引擎这边按文件名统一排除。
    """
    root = tmp_path / "proj"
    (root / "app").mkdir(parents=True)
    (root / "app" / "a.py").write_text("import os\nx=os.environ.get('REAL')\n", encoding="utf-8")
    (root / "app" / "env_audit.py").write_text(
        '"""示例：os.environ.get("X") 是示意，不是真实读取。"""\n', encoding="utf-8")
    (root / ".env").write_text("REAL=1\n", encoding="utf-8")
    result = audit(root, {"code_dirs": ["app"]})
    assert result.code_keys == {"REAL"}, result.code_keys
    assert "X" not in result.missing


def test_deprecated_alias(code_project):
    (code_project / "app" / "b.py").write_text(
        "import os\nos.environ.get('OLD_NAME')\n", encoding="utf-8")
    (code_project / ".env").write_text("NEW_NAME=x\n", encoding="utf-8")
    result = audit(code_project, {
        "code_dirs": ["app"],
        "deprecated_aliases": {"OLD_NAME": "NEW_NAME"},
    })
    assert "OLD_NAME" not in result.missing
    assert "NEW_NAME" not in result.dead


def test_scan_failure_is_fail_closed(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    result = audit(root, {"code_dirs": ["nothing-here"]})
    assert result.scan_failed is True


def test_scan_failure_never_reports_dead_or_missing(tmp_path):
    """扫不到代码时必须闭嘴，不能把已配键说成「可删的死键」。

    这条是踩过坑才加的：ZK-AI 上 `code_dirs` 配错时，引擎把 48 个凭据键
    （含 ZKAI_DATABASE_URL、ZKAI_ADMIN_TOKEN）全报成「死键，确认后可删」，
    因为 dead = env_keys - 空集。照做网关当场全瘫。
    """
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".env").write_text(
        "ZKAI_DATABASE_URL=sqlite:///x\nZKAI_ADMIN_TOKEN=token\nNVIDIA_API_KEY=key\n",
        encoding="utf-8")
    result = audit(root, {"code_dirs": ["nothing-here"]})
    assert result.scan_failed is True
    assert result.dead == set()
    assert result.missing == set()
    assert result.missing_required == set()


def test_env_files_multi_source(tmp_path):
    """env_files 支持多源：.env 与 .env.server 的键都算「已配置」。"""
    root = tmp_path / "proj"
    (root / "app").mkdir(parents=True)
    (root / "app" / "a.py").write_text(
        "import os\nx=os.environ.get('A')\ny=os.environ.get('SMTP_PORT')\n", encoding="utf-8")
    (root / ".env").write_text("A=1\n", encoding="utf-8")
    (root / ".env.server").write_text("SMTP_PORT=25\nDEAD_ONLY_HERE=nobody\n", encoding="utf-8")
    result = audit(root, {"code_dirs": ["app"]}, env_files=[".env", ".env.server"])
    # 两个文件的键都进 env_keys：SMTP_PORT 不因「.env 里没有」被误判缺失
    assert result.env_keys == {"A", "SMTP_PORT", "DEAD_ONLY_HERE"}
    assert result.missing == set()
    assert result.dead == {"DEAD_ONLY_HERE"}

    # 只读 .env 时会漏掉 .env.server 的键——这正是那 14 个 server_only 键
    # 「重建服务器时静默消失」的成因
    single = audit(root, {"code_dirs": ["app"]})
    assert "SMTP_PORT" in single.missing


def test_env_keys_never_leak_values(code_project, secret_values, capsys):
    # read_env_file_keys 只返回键名
    keys = read_env_file_keys(code_project / ".env")
    assert keys == {"ADMIN_API_KEY", "JWT_SECRET", "SMTP_HOST", "DEAD_KEY"}
    text = str(sorted(keys))
    for secret in secret_values:
        assert secret not in text


def test_export_variants_and_comments(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# comment\n"
        "export EXPORTED=1\n"
        "  INDENTED=2\n"
        "lowercase=ignored\n"
        "EMPTY=\n",
        encoding="utf-8",
    )
    assert read_env_file_keys(env) == {"EXPORTED", "INDENTED", "EMPTY"}


def test_scan_skips_venv(tmp_path):
    root = tmp_path / "p"
    (root / "app").mkdir(parents=True)
    (root / ".venv" / "lib").mkdir(parents=True)
    (root / "app" / "m.py").write_text("import os\nos.environ.get('REAL')\n", encoding="utf-8")
    (root / ".venv" / "lib" / "m.py").write_text(
        "import os\nos.environ.get('VENV_KEY')\n", encoding="utf-8")
    assert scan_code_keys(root, ["."]) == {"REAL"}
