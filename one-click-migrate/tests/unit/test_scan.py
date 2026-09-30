"""scan 层测试：条目解析、展开规则、快照哈希。"""

from __future__ import annotations

import sqlite3

import pytest

from migrate_engine.manifest import Item
from migrate_engine.scan import rel_posix, resolve_item, sha256_file, snapshot_files


def test_file_item_snapshot(project_with_manifest):
    item = Item(id="env", path=".env")
    resolved = resolve_item(project_with_manifest, item)
    assert not resolved.missing
    assert list(resolved.archive_names) == [".env"]
    snaps = snapshot_files(resolved, project_with_manifest)
    assert snaps[0].sha256 == sha256_file(project_with_manifest / ".env")


def test_missing_item_is_flagged(fake_project):
    (fake_project / ".env").unlink()
    resolved = resolve_item(fake_project, Item(id="gone", path=".env"))
    assert resolved.missing


def test_dir_item_expands_and_skips_venv(fake_project):
    (fake_project / "pkg").mkdir()
    (fake_project / "pkg" / "a.txt").write_text("a", encoding="utf-8")
    (fake_project / "pkg" / "node_modules").mkdir()
    (fake_project / "pkg" / "node_modules" / "junk.js").write_text("j", encoding="utf-8")
    resolved = resolve_item(fake_project, Item(id="pkg", path="pkg", item_type="dir"))
    names = sorted(resolved.archive_names)
    assert names == ["pkg/a.txt"], names


def test_glob_item(fake_project):
    (fake_project / "docs").mkdir()
    (fake_project / "docs" / "a.md").write_text("a", encoding="utf-8")
    (fake_project / "docs" / "b.txt").write_text("b", encoding="utf-8")
    resolved = resolve_item(fake_project, Item(id="md", path="docs/*.md", item_type="glob"))
    assert list(resolved.archive_names) == ["docs/a.md"]


def test_symlink_file_item_keeps_declared_name(fake_project):
    target = fake_project / "real.txt"
    target.write_text("real", encoding="utf-8")
    link = fake_project / "link.txt"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("平台不支持符号链接")
    resolved = resolve_item(fake_project, Item(id="lk", path="link.txt"))
    assert not resolved.missing
    assert list(resolved.archive_names) == ["link.txt"]  # 包内名不跟 resolve 跑掉
    assert any("符号链接" in n for n in resolved.notes)


def test_sqlite_item_resolves_db_file(project_with_manifest):
    resolved = resolve_item(project_with_manifest, Item(id="db", path="data/app.db", item_type="sqlite"))
    assert not resolved.missing
    assert list(resolved.archive_names) == ["data/app.db"]
    # 快照哈希可计算（打通 sqlite 可用性）
    snaps = snapshot_files(resolved, project_with_manifest)
    assert snaps[0].bytes > 0
    conn = sqlite3.connect(project_with_manifest / "data" / "app.db")
    conn.execute("select count(*) from t")
    conn.close()


def test_rel_posix_is_lexical(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    assert rel_posix(root / "a" / "b.txt", root) == "a/b.txt"
