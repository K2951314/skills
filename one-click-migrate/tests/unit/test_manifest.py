"""manifest 解析与校验测试。"""

from __future__ import annotations

import pytest

from migrate_engine.manifest import (
    MANIFEST_REL,
    ManifestError,
    load_manifest,
    scaffold_manifest,
    select_items,
    validate_data,
)


def test_valid_manifest_loads(project_with_manifest):
    m = load_manifest(project_with_manifest)
    assert m.project == "fake-proj"
    assert [i.id for i in m.items] == ["env-file", "local-config", "app-db", "ops-notes"]
    assert m.artifacts_dir == ".migrate"
    assert m.merge_include == [".env", "config.local.json"]
    assert m.rebuild == ["python -m venv .venv"]
    assert m.verify == ["python -m pytest tests/ -q"]
    assert m.env_audit["server_only"] == ["SMTP_HOST"]


def test_missing_manifest_raises(fake_project):
    with pytest.raises(ManifestError):
        load_manifest(fake_project)


def test_bad_enum_is_reported(fake_project):
    (fake_project / MANIFEST_REL).write_text(
        """
schema_version = 1
project = "p"
[[items]]
id = "a"
path = ".env"
class = "critical"
""",
        encoding="utf-8",
    )
    with pytest.raises(ManifestError) as excinfo:
        load_manifest(fake_project)
    assert "class" in str(excinfo.value)


def test_future_schema_version_refused(fake_project):
    (fake_project / MANIFEST_REL).write_text(
        """
schema_version = 99
project = "p"
[[items]]
id = "a"
path = ".env"
""",
        encoding="utf-8",
    )
    with pytest.raises(ManifestError) as excinfo:
        load_manifest(fake_project)
    assert excinfo.value.exit_code == 8
    assert "更新" in str(excinfo.value)


@pytest.mark.parametrize("bad_path", ["/etc/passwd", "C:\\Windows\\x", "../outside", "a//b", "\\\\srv\\share"])
def test_path_must_be_inside_project(bad_path):
    problems, _ = validate_data({
        "schema_version": 1,
        "project": "p",
        "items": [{"id": "a", "path": bad_path}],
    })
    assert any("path" in p for p in problems), f"{bad_path} 未被拒绝：{problems}"


def test_duplicate_ids_reported():
    problems, _ = validate_data({
        "schema_version": 1,
        "project": "p",
        "items": [
            {"id": "a", "path": ".env"},
            {"id": "a", "path": ".env.server"},
        ],
    })
    assert any("重复" in p for p in problems)


def test_server_scope_needs_remote_path():
    problems, _ = validate_data({
        "schema_version": 1,
        "project": "p",
        "items": [{"id": "a", "path": "x", "scope": "server"}],
    })
    assert any("remote_path" in p for p in problems)


def test_unknown_top_level_field_warns():
    problems, m = validate_data({
        "schema_version": 1,
        "project": "p",
        "itms": [],
        "items": [{"id": "a", "path": ".env"}],
    })
    assert m.project == "p"
    assert any("itms" in p for p in problems)


def test_profile_selects_and_rejects_unknown(project_with_manifest):
    m = load_manifest(project_with_manifest)
    only = select_items(m, "machine-only")
    assert [i.id for i in only] == ["env-file", "local-config", "ops-notes"]
    with pytest.raises(ManifestError):
        select_items(m, "nope")


def test_scaffold_roundtrip(fake_project):
    text = scaffold_manifest("my-proj")
    (fake_project / MANIFEST_REL).write_text(text, encoding="utf-8")
    m = load_manifest(fake_project)
    assert m.project == "my-proj"
    assert m.items, "草稿应自带示例条目"
    problems, _ = validate_data({"schema_version": 1, "project": "x", "items": []})
    assert any("items" in p for p in problems)


def test_scaffold_rejects_bad_slug():
    with pytest.raises(ManifestError):
        scaffold_manifest("Not_A_Slug")
