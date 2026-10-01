"""server 采集/上传测试：fake ssh（monkeypatch），不碰真实服务器。"""

from __future__ import annotations

import io
import json
import sqlite3
import tarfile
import zipfile
from pathlib import Path

import pytest

from migrate_engine import platform as plat
from migrate_engine.cli import main
from migrate_engine.manifest import load_manifest
from migrate_engine.pack import PACKAGE_FORMAT
from migrate_engine.server import (
    ServerError,
    build_server_package,
    capture_server,
    upload_stage,
    validate_target,
)
from migrate_engine.unpack import open_package

PW = "server-passphrase-123"

SERVER_MANIFEST = """
schema_version = 1
project = "fake-srv"
artifacts_dir = ".migrate"

[[items]]
id = "env-etc"
path = "etc-sq.env"
remote_path = "/etc/sq.env"
class = "required"
scope = "server"
on_missing = "block"
capture = "file"

[[items]]
id = "caddy"
path = "Caddyfile"
remote_path = "/etc/caddy/Caddyfile"
class = "recommended"
scope = "server"
capture = "file"

[[items]]
id = "main-db"
path = "sqdb.dump"
class = "required"
scope = "server"
capture = "pg_dump"
pg_user = "postgres"
pg_db = "sqdb"
"""


@pytest.fixture()
def server_project(tmp_path: Path) -> Path:
    root = tmp_path / "srvproj"
    (root / ".migrate").mkdir(parents=True)
    (root / ".migrate" / "manifest.toml").write_text(SERVER_MANIFEST, encoding="utf-8")
    return root


@pytest.fixture()
def fake_ssh(monkeypatch):
    """假的 ssh：按输入脚本体分派，返回预置应答。"""
    calls: list[dict] = []

    def fake_run_binary(argv, *, timeout=60.0, env=None, cwd=None, input_bytes=None):
        script = (input_bytes or b"").decode("utf-8")
        calls.append({"argv": argv, "script": script, "timeout": timeout})
        if "pg_dump" in script:
            return 0, b"PGDMP\x00\x00fake-dump-bytes", b""
        if "show server_version" in script:
            return 0, (
                "pulled_at=2026-09-30T12:00:00Z\npostgres=16.15\n"
                "python=Python3.12.3\ncaddy=v2.11.4\n"
            ).encode("utf-8"), b""
        if "cat --" in script:
            path = script.split('path="$1"', 1)[1] if False else None
            # 从 argv 末位拿远端路径
            remote = argv[-1]
            if remote == "/etc/sq.env":
                return 0, b"JWT_SECRET=from-server\nSMTP_HOST=smtp.example.com\n", b""
            if remote == "/etc/caddy/Caddyfile":
                return 0, b"example.com {\n  reverse_proxy 127.0.0.1:8001\n}\n", b""
            return 3, b"", b"cannot read"
        return 0, b"", b""

    monkeypatch.setattr(plat, "run_binary", fake_run_binary)
    return calls


def test_target_validation():
    assert validate_target("ubuntu@203.0.113.10") == "ubuntu@203.0.113.10"
    for bad in ["203.0.113.10", "ubuntu", "ubuntu@host; rm -rf /", "-oProxyCommand=x", ""]:
        with pytest.raises(ServerError):
            validate_target(bad)


def test_ssh_uses_batchmode_and_positional_args(server_project, fake_ssh):
    manifest = load_manifest(server_project)
    items = list(manifest.items)
    capture_server("ubuntu@10.0.0.9", manifest, items)
    pg_call = [c for c in fake_ssh if "pg_dump" in c["script"]][0]
    assert "BatchMode=yes" in pg_call["argv"]
    assert "StrictHostKeyChecking=yes" in pg_call["argv"]
    assert pg_call["argv"][-2:] == ["postgres", "sqdb"]  # 库名走位置参数
    cat_call = [c for c in fake_ssh if "cat --" in c["script"]][0]
    assert cat_call["argv"][-1] == "/etc/sq.env"
    # 用户输入不进脚本体（脚本是常量模板）
    assert "sqdb" not in cat_call["script"]
    assert "/etc/sq.env" not in cat_call["script"]


def test_capture_collects_and_validates_dump(server_project, fake_ssh):
    manifest = load_manifest(server_project)
    result = capture_server("ubuntu@10.0.0.9", manifest, list(manifest.items))
    names = [e.name for e in result.entries]
    assert "etc-sq.env" in names and "Caddyfile" in names and "sqdb.dump" in names
    assert "postgres=16.15" in result.metadata
    # 值只在内存，不进 notes
    assert not any("JWT_SECRET" in n for n in result.notes)


def test_bad_dump_header_rejected(server_project, monkeypatch):
    def fake_run_binary(argv, *, timeout=60.0, env=None, cwd=None, input_bytes=None):
        script = (input_bytes or b"").decode("utf-8")
        if "pg_dump" in script:
            return 0, b"NOT-A-DUMP", b""
        if "show server_version" in script:
            return 0, b"pulled_at=x\n", b""
        return 0, b"JWT_SECRET=x\n", b""

    monkeypatch.setattr(plat, "run_binary", fake_run_binary)
    manifest = load_manifest(server_project)
    with pytest.raises(ServerError) as exc:
        capture_server("ubuntu@10.0.0.9", manifest, list(manifest.items))
    assert "PGDMP" in str(exc.value)


def test_empty_dump_rejected(server_project, monkeypatch):
    def fake_run_binary(argv, *, timeout=60.0, env=None, cwd=None, input_bytes=None):
        script = (input_bytes or b"").decode("utf-8")
        if "pg_dump" in script:
            return 0, b"", b""
        if "show server_version" in script:
            return 0, b"pulled_at=x\n", b""
        return 0, b"JWT_SECRET=x\n", b""

    monkeypatch.setattr(plat, "run_binary", fake_run_binary)
    manifest = load_manifest(server_project)
    with pytest.raises(ServerError):
        capture_server("ubuntu@10.0.0.9", manifest, list(manifest.items))


def test_server_package_roundtrip_and_merge_refusal(server_project, fake_ssh, tmp_path):
    manifest = load_manifest(server_project)
    result = capture_server("ubuntu@10.0.0.9", manifest, list(manifest.items))
    pkg = build_server_package(result, manifest, PW, server_project / ".migrate")
    info, files = open_package(pkg, PW)
    assert info["kind"] == "server"
    assert "ENV-METADATA.txt" in files
    target_root = tmp_path / "newproj"
    target_root.mkdir()
    from migrate_engine.unpack import restore_package

    with pytest.raises(Exception) as exc:
        restore_package(pkg, PW, target_root)
    assert exc.value.exit_code == 7


def test_upload_refuses_workspace_package(project_with_manifest, fake_ssh, tmp_path):
    from migrate_engine.manifest import select_items
    from migrate_engine.pack import export_package

    manifest = load_manifest(project_with_manifest)
    items = [i for i in select_items(manifest, None) if i.scope in {"workspace", "both"}]
    pkg = export_package(project_with_manifest, manifest, items,
                         kind="workspace", passphrase=PW)
    with pytest.raises(ServerError):
        upload_stage(pkg.path, PW, "ubuntu@10.0.0.9")


def test_upload_streams_tar_and_lists_commands(server_project, fake_ssh):
    manifest = load_manifest(server_project)
    result = capture_server("ubuntu@10.0.0.9", manifest, list(manifest.items))
    pkg = build_server_package(result, manifest, PW, server_project / ".migrate")
    hints = {i.path: {"remote_path": i.remote_path, "pg_db": i.pg_db}
             for i in manifest.items if i.scope in {"server", "both"}}
    commands = upload_stage(pkg, PW, "ubuntu@10.0.0.8", hints=hints)
    joined = "\n".join(commands)
    assert "pg_restore" in joined
    assert "-d sqdb" in joined
    assert "/etc/sq.env" in joined
    assert "chmod 640" in joined
    assert "rm -rf /tmp/oc-migrate-stage" in joined


def test_cli_server_export(server_project, fake_ssh, capsys):
    pw_file = server_project / ".migrate" / "pw.txt"
    pw_file.write_text(PW + "\n", encoding="utf-8")
    code = main(["--root", str(server_project), "server", "export",
                 "--target", "ubuntu@10.0.0.9", "--passphrase-file", str(pw_file)])
    out = capsys.readouterr().out
    assert code == 0
    assert "fake-srv-server-" in out
    assert "禁止合并" in out
    assert list((server_project / ".migrate").glob("*.enc"))
