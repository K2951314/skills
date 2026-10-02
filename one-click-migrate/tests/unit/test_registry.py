"""注册表模块测试：加载、校验、路径展开、过滤。"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from migrate_engine.registry import (
    DEFAULT_REGISTRY_PATH,
    REGISTRY_SCHEMA_VERSION,
    RegistryError,
    expand_path,
    filter_entries,
    load_registry,
    scaffold_registry,
    validate_registry,
)


# ── expand_path ──────────────────────────────────────────────────────────


def test_expand_path_tilde(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    result = expand_path("~/projects/foo")
    assert result == tmp_path / "projects" / "foo"


def test_expand_path_env_var_posix(monkeypatch, tmp_path):
    monkeypatch.setenv("MYROOT", str(tmp_path))
    result = expand_path("$MYROOT/foo")
    assert result == tmp_path / "foo"


def test_expand_path_env_var_windows(monkeypatch, tmp_path):
    monkeypatch.setenv("MYROOT", str(tmp_path))
    result = expand_path("%MYROOT%/foo")
    assert result == tmp_path / "foo"


def test_expand_path_absolute():
    result = expand_path("/tmp/foo")
    assert result == Path("/tmp/foo")


# ── validate_registry ───────────────────────────────────────────────────


def _valid_data():
    return {
        "schema_version": 1,
        "projects": [
            {"name": "alpha", "path": "~/alpha"},
            {"name": "beta", "path": "/tmp/beta", "profile": "machine-only"},
        ],
    }


def test_validate_registry_ok():
    problems, registry = validate_registry(_valid_data())
    assert problems == []
    assert registry.version == 1
    assert len(registry.entries) == 2
    assert registry.entries[0].name == "alpha"
    assert registry.entries[1].profile == "machine-only"


def test_validate_registry_future_version():
    data = _valid_data()
    data["schema_version"] = REGISTRY_SCHEMA_VERSION + 1
    with pytest.raises(RegistryError) as exc_info:
        validate_registry(data)
    assert "高于引擎支持" in str(exc_info.value)


def test_validate_registry_missing_projects():
    problems, registry = validate_registry({"schema_version": 1})
    assert any("projects" in p for p in problems)
    assert registry.entries == []


def test_validate_registry_bad_name():
    data = _valid_data()
    data["projects"][0]["name"] = "Bad Name!"
    problems, _ = validate_registry(data)
    assert any("name" in p for p in problems)


def test_validate_registry_dup_name():
    data = {
        "schema_version": 1,
        "projects": [
            {"name": "dup", "path": "~/a"},
            {"name": "dup", "path": "~/b"},
        ],
    }
    problems, _ = validate_registry(data)
    assert any("重复" in p for p in problems)


def test_validate_registry_unknown_field_in_entry():
    data = _valid_data()
    data["projects"][0]["pat"] = "~/alpha"  # 拼错
    problems, _ = validate_registry(data)
    assert any("pat" in p for p in problems)


# ── load_registry ────────────────────────────────────────────────────────


def test_load_registry_file_not_found(tmp_path):
    with pytest.raises(RegistryError) as exc_info:
        load_registry(tmp_path / "missing.toml")
    assert "没找到注册表" in str(exc_info.value)
    assert "batch init" in str(exc_info.value)


def test_load_registry_bad_toml(tmp_path):
    p = tmp_path / "reg.toml"
    p.write_text("not = valid = toml = {{{", encoding="utf-8")
    with pytest.raises(RegistryError) as exc_info:
        load_registry(p)
    assert "不是合法 TOML" in str(exc_info.value)


def test_load_registry_bad_utf8(tmp_path):
    p = tmp_path / "reg.toml"
    p.write_bytes(b"\xff\xfe\x00\x01 not utf-8")
    with pytest.raises(RegistryError) as exc_info:
        load_registry(p)
    assert "UTF-8" in str(exc_info.value)


def test_load_registry_ok(tmp_path):
    p = tmp_path / "reg.toml"
    p.write_text(textwrap.dedent("""
        schema_version = 1
        [[projects]]
        name = "alpha"
        path = "~/alpha"
        [[projects]]
        name = "beta"
        path = "/tmp/beta"
    """), encoding="utf-8")
    registry = load_registry(p)
    assert registry.path == p
    assert len(registry.entries) == 2
    assert registry.entries[0].name == "alpha"


# ── filter_entries ───────────────────────────────────────────────────────


def _make_registry(entries):
    from migrate_engine.registry import Registry, RegistryEntry
    return Registry(
        version=1,
        entries=[RegistryEntry(name=n, path=p, resolved=Path(p), profile=prof)
                 for n, p, prof in entries],
    )


def test_filter_entries_only():
    reg = _make_registry([("a", "/a", None), ("b", "/b", None), ("c", "/c", None)])
    result = filter_entries(reg, ["a", "c"], None)
    assert [e.name for e in result] == ["a", "c"]


def test_filter_entries_exclude():
    reg = _make_registry([("a", "/a", None), ("b", "/b", None), ("c", "/c", None)])
    result = filter_entries(reg, None, ["b"])
    assert [e.name for e in result] == ["a", "c"]


def test_filter_entries_only_unknown():
    reg = _make_registry([("a", "/a", None)])
    with pytest.raises(RegistryError) as exc_info:
        filter_entries(reg, ["xyz"], None)
    assert "没有的项目" in str(exc_info.value)


def test_filter_entries_exclude_unknown():
    reg = _make_registry([("a", "/a", None)])
    with pytest.raises(RegistryError) as exc_info:
        filter_entries(reg, None, ["xyz"])
    assert "没有的项目" in str(exc_info.value)


# ── scaffold_registry ────────────────────────────────────────────────────


def test_scaffold_registry_is_valid():
    text = scaffold_registry()
    assert "schema_version = 1" in text
    assert "[[projects]]" in text
    assert "name =" in text
    assert "path =" in text
