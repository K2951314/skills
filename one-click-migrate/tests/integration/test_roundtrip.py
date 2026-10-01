"""端到端：workspace 导出→导入 roundtrip、SQLite 快照、冲突、回滚、journal。"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

from migrate_engine.cli import main
from migrate_engine.crypto import encrypt_blob
from migrate_engine.manifest import load_manifest
from migrate_engine.pack import KIND_SERVER, ExportError, export_package
from migrate_engine.scan import sha256_file
from migrate_engine.unpack import PackageError, open_package, restore_package

PW = "integration-passphrase-123"
PW2 = "integration-passphrase-456"


def _export(project_with_manifest, passphrase=PW):
    manifest = load_manifest(project_with_manifest)
    from migrate_engine.manifest import select_items

    items = [i for i in select_items(manifest, None) if i.scope in {"workspace", "both"}]
    return export_package(project_with_manifest, manifest, items,
                          kind="workspace", passphrase=passphrase)


def test_export_creates_encrypted_package(project_with_manifest):
    result = _export(project_with_manifest)
    assert result.path.exists()
    assert result.path.name.startswith("fake-proj-workspace-")
    blob = result.path.read_bytes()
    assert b"JWT" not in blob and b"super-secret" not in blob  # 密文不泄内容
    info, files = open_package(result.path, PW)
    assert info["kind"] == "workspace"
    assert {f for f in files} == {".env", "config.local.json", "data/app.db", "_ops-notes.md"}


def test_export_readback_deletes_bad_package(project_with_manifest, monkeypatch):
    import migrate_engine.unpack as unpack_mod

    monkeypatch.setattr(unpack_mod, "open_package",
                        lambda *a, **k: (_ for _ in ()).throw(PackageError("boom")))
    with pytest.raises(ExportError):
        _export(project_with_manifest)
    assert list((project_with_manifest / ".migrate").glob("*.enc")) == []


def test_roundtrip_into_fresh_clone(project_with_manifest, tmp_path):
    result = _export(project_with_manifest)
    fresh = tmp_path / "clone"
    fresh.mkdir()
    # 新克隆：只有 gitignore 与代码，没有任何被排除资产
    (fresh / ".gitignore").write_text(".env\n", encoding="utf-8")
    info, plan, report = restore_package(result.path, PW, fresh, on_conflict="skip")
    assert report is not None
    assert (fresh / ".env").read_text(encoding="utf-8") == \
        (project_with_manifest / ".env").read_text(encoding="utf-8")
    assert (fresh / "config.local.json").is_file()
    assert (fresh / "_ops-notes.md").is_file()
    for name in report.written:
        assert sha256_file(fresh / name) == sha256_file(project_with_manifest / name) \
            or name == "data/app.db"  # sqlite 快照与原件不同（一致性副本）


def test_sqlite_snapshot_is_consistent_and_sidecars_cleared(project_with_manifest, tmp_path):
    # 源库旁边放一个 stale WAL，里面是「旧数据库」的帧
    src_db = project_with_manifest / "data" / "app.db"
    (project_with_manifest / "data" / "app.db-wal").write_bytes(b"stale-wal-frames")
    result = _export(project_with_manifest)
    # 快照里可读出数据（backup API 保证一致）
    info, files = open_package(result.path, PW)
    snap = files["data/app.db"][0]
    target_db = tmp_path / "clone" / "data" / "app.db"
    target_db.parent.mkdir(parents=True)
    target_db.write_bytes(snap)
    conn = sqlite3.connect(target_db)
    assert conn.execute("select count(*) from t").fetchone()[0] == 1
    conn.close()
    del src_db
    # 新机已有旧库 + stale sidecar：导入先清 sidecar 再落新库
    clone = tmp_path / "clone2"
    clone.mkdir()
    (clone / "data").mkdir()
    (clone / "data" / "app.db").write_bytes(b"old-db-bytes")
    (clone / "data" / "app.db-wal").write_bytes(b"stale-wal-frames")
    info, plan, report = restore_package(result.path, PW, clone,
                                         on_conflict="overwrite")
    assert not (clone / "data" / "app.db-wal").exists()
    assert any("残留" in w for w in report.warnings)
    conn = sqlite3.connect(clone / "data" / "app.db")
    assert conn.execute("select count(*) from t").fetchone()[0] == 1
    conn.close()


def test_import_default_skips_existing(project_with_manifest):
    result = _export(project_with_manifest)
    info, plan, report = restore_package(result.path, PW, project_with_manifest,
                                         on_conflict="skip")
    assert report.written == []
    assert len(report.skipped) == len(info["items"])


def test_import_overwrite_backs_up(project_with_manifest):
    result = _export(project_with_manifest)
    original = (project_with_manifest / ".env").read_bytes()
    (project_with_manifest / ".env").write_text("JWT_SECRET=changed\n", encoding="utf-8")
    info, plan, report = restore_package(result.path, PW, project_with_manifest,
                                         on_conflict="overwrite")
    assert ".env" in report.written
    assert (project_with_manifest / ".env").read_bytes() == original
    backup = report.backups / ".env"
    assert backup.is_file()
    assert b"changed" in backup.read_bytes()


def test_keep_both_writes_incoming(project_with_manifest):
    result = _export(project_with_manifest)
    info, plan, report = restore_package(result.path, PW, project_with_manifest,
                                         on_conflict="keep-both")
    incoming = list(project_with_manifest.glob(".env.incoming-*"))
    assert incoming, report.kept_both
    assert (project_with_manifest / ".env").read_text(encoding="utf-8").startswith("JWT_SECRET=super")


def test_tracked_files_blocked(project_with_manifest, tmp_path):
    result = _export(project_with_manifest)
    (project_with_manifest / ".env").unlink()
    info, plan, report = restore_package(
        result.path, PW, project_with_manifest, on_conflict="skip",
        tracked={".env"})
    assert report.written == []
    assert any(".env" in s for s in report.skipped)


def test_server_package_merge_refused(project_with_manifest, tmp_path):
    manifest = load_manifest(project_with_manifest)
    from migrate_engine.manifest import select_items

    items = [i for i in select_items(manifest, None) if i.scope in {"workspace", "both"}]
    # 手工造一个 server 包：复用 build_payload_zip + encrypt
    import migrate_engine.pack as pack_mod

    entries, _ = pack_mod.collect_entries(project_with_manifest, manifest, items)
    zip_bytes = pack_mod.build_payload_zip(entries, kind=KIND_SERVER,
                                           project=manifest.project,
                                           rebuild=[], verify=[])
    pkg = tmp_path / "server.enc"
    pkg.write_bytes(encrypt_blob(zip_bytes, PW))
    with pytest.raises(PackageError) as exc:
        restore_package(pkg, PW, tmp_path / "x")
    assert exc.value.exit_code == 7


def test_rollback_on_midway_failure(project_with_manifest, tmp_path, monkeypatch):
    result = _export(project_with_manifest)
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / ".env").write_text("ORIGINAL\n", encoding="utf-8")

    import migrate_engine.unpack as unpack_mod
    from migrate_engine.journal import atomic_commit

    original = atomic_commit
    calls = {"n": 0}

    def flaky(*, tmp, dst, data, sha256):
        calls["n"] += 1
        if calls["n"] == 1:
            return original(tmp=tmp, dst=dst, data=data, sha256=sha256)
        raise OSError("disk on fire")

    monkeypatch.setattr(unpack_mod, "atomic_commit", flaky)
    with pytest.raises(OSError):
        restore_package(result.path, PW, clone, on_conflict="overwrite")
    # 第一个已落盘的文件被回滚成原内容
    assert (clone / ".env").read_text(encoding="utf-8") == "ORIGINAL\n"


def test_journal_recover_after_crash(project_with_manifest, tmp_path):
    from migrate_engine.journal import recover

    result = _export(project_with_manifest)
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / ".env").write_text("ORIGINAL\n", encoding="utf-8")
    info, plan, report = restore_package(result.path, PW, clone, on_conflict="overwrite")
    journal = report.journal
    # 场景一：有备份的已提交文件被破坏 → 从备份恢复
    (clone / ".env").write_text("corrupted")
    # 场景二：导入产生的新文件被破坏 → 恢复到「不存在」（删除）
    (clone / "config.local.json").write_text("corrupted")
    actions = recover(clone / ".migrate", journal)
    assert any(".env" in a and "恢复" in a for a in actions)
    assert any("config.local.json" in a and "删除" in a for a in actions)
    # 备份 = 导入前的 ORIGINAL；恢复语义是「回到导入前」，不是「回到包里」
    assert (clone / ".env").read_text(encoding="utf-8") == "ORIGINAL\n"
    assert not (clone / "config.local.json").exists()


def test_dry_run_writes_nothing(project_with_manifest, tmp_path):
    result = _export(project_with_manifest)
    clone = tmp_path / "clone"
    clone.mkdir()
    info, plan, report = restore_package(result.path, PW, clone, dry_run=True)
    assert report is None
    assert list(clone.iterdir()) == []


def test_verify_cli(project_with_manifest, capsys):
    result = _export(project_with_manifest)
    code = main(["--root", str(project_with_manifest), "verify", str(result.path),
                 "--passphrase-file", _pw_file(project_with_manifest)])
    out = capsys.readouterr().out
    assert code == 0
    assert "校验通过" in out


def test_import_cli_conflict_exit_code(project_with_manifest, capsys):
    """有文件成功写入、另有目标已存在被跳过 → 退出码 3 并提示 overwrite。"""
    result = _export(project_with_manifest)
    fresh = tmp_path = project_with_manifest.parent / "clone-with-one-existing"
    fresh.mkdir()
    # .env 已在（git clone 就有），其余不存在 → 会写 3 个、跳 1 个
    (fresh / ".gitignore").write_text(".env\n", encoding="utf-8")
    (fresh / ".env").write_text("JWT_SECRET=already-here\n", encoding="utf-8")
    code = main(["--root", str(project_with_manifest), "import", str(result.path),
                 "--out", str(fresh),
                 "--passphrase-file", _pw_file(project_with_manifest)])
    out = capsys.readouterr().out
    assert code == 3  # 目标已存在 → 冲突退出码
    assert "--on-conflict overwrite" in out


def test_import_zero_written_is_an_error(project_with_manifest, capsys):
    """一个文件都没落地必须报错，不能静默 exit 0（A16）。

    白名单取自**目标项目**的 manifest。把一份白名单指向不存在路径的 manifest
    放进目标目录 → 包内所有成员都被 blocked。旧逻辑在这种情况下 exit 0。
    """
    result = _export(project_with_manifest)
    clone = project_with_manifest.parent / "clone-empty"
    (clone / ".migrate").mkdir(parents=True)
    (clone / ".migrate" / "manifest.toml").write_text(
        'schema_version = 1\nproject = "fake-proj"\n\n[[items]]\nid = "env-file"\n'
        'path = ".env"\n\n[merge]\ninclude = ["nonexistent.txt"]\n',
        encoding="utf-8")
    code = main(["--root", str(clone), "import", str(result.path),
                 "--passphrase-file", _pw_file(project_with_manifest)])
    out = capsys.readouterr().out
    assert code == 3
    assert "一个文件都没有写入" in out
    # 说清是哪一类原因挡的，别让用户猜
    assert "白名单" in out
    assert not (clone / ".env").exists()


def test_whitelist_that_matches_nothing_is_not_silently_opened(project_with_manifest, tmp_path):
    """配了 [merge] 但一条都不匹配 = 什么都不许合并，不是「无限制」。"""
    result = _export(project_with_manifest)
    clone = tmp_path / "clone-wl"
    (clone / ".migrate").mkdir(parents=True)
    (clone / ".migrate" / "manifest.toml").write_text(
        'schema_version = 1\nproject = "fake-proj"\n\n[[items]]\nid = "env-file"\n'
        'path = ".env"\n\n[merge]\ninclude = ["nonexistent.txt"]\n',
        encoding="utf-8")
    info, plan, report = restore_package(result.path, PW, clone,
                                         on_conflict="skip",
                                         merge_whitelist=["nonexistent.txt"])
    assert report.written == []
    assert all(p.action == "blocked" for p in plan)
    assert not (clone / ".env").exists()


def test_import_cli_wrong_passphrase_exit_code(project_with_manifest, tmp_path, capsys):
    result = _export(project_with_manifest)
    clone = tmp_path / "clone"
    clone.mkdir()
    pw_file = tmp_path / "pw.txt"
    pw_file.write_text(PW2 + "\n", encoding="utf-8")
    code = main(["--root", str(clone), "import", str(result.path),
                 "--passphrase-file", str(pw_file)])
    out = capsys.readouterr().out
    assert code == 4
    assert "口令" in out


def test_import_cli_pick_lists_and_selects(project_with_manifest, monkeypatch, capsys):
    """--pick：不给包路径时列出产物目录里的包，按编号选。

    这是双击 .cmd「一键换机」入口的必需能力：用户手上只有一个目录，
    不是一条路径。选包的逻辑必须在引擎里做完——旧实现用 for /f 把路径回传
    给 cmd，被 PowerShell 管道送来的 BOM 弄坏过。
    """
    result = _export(project_with_manifest)
    answers = iter(["1"])
    monkeypatch.setattr("migrate_engine.cli._read_line", lambda prompt: next(answers))
    clone = project_with_manifest.parent / "clone-pick"
    clone.mkdir()
    # --root 是「包在哪、manifest 怎么说」的一方；--out 才是写入目标。
    # 新机器的空目录里既没有包也没有 manifest，两者必须分开。
    code = main(["--root", str(project_with_manifest), "import", "--pick",
                 "--out", str(clone),
                 "--passphrase-file", _pw_file(project_with_manifest)])
    out = capsys.readouterr().out
    assert code == 0
    # 列出了包并等待选择，然后按编号导入了选中的那个
    assert "个迁移包" in out
    assert ".enc" in out
    assert (clone / ".env").is_file()


def test_import_cli_pick_empty_dir_is_clear(tmp_path):
    """产物目录里没有包时给可操作的错误，不是崩。"""
    code = main(["--root", str(tmp_path), "import", "--pick"])
    assert code == 2


def test_import_without_package_tells_you_about_pick(tmp_path):
    code = main(["--root", str(tmp_path), "import"])
    assert code == 2


def _pw_file(root: Path) -> str:
    p = root / ".migrate" / "pw.txt"
    p.write_text(PW + "\n", encoding="utf-8")
    return str(p)
