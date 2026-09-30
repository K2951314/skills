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
