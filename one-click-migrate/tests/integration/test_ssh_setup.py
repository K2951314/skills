"""SSH 免密配置测试：fake ssh/ssh-keygen/ssh-copy-id（monkeypatch），不碰真实网络。

覆盖 platform 层（密钥发现、连接探测）与 ssh_setup 层（密钥生成、公钥推送、验证）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from migrate_engine import platform as plat
from migrate_engine.cli import main
from migrate_engine.ssh_setup import (
    SshSetupError,
    ensure_key,
    push_public_key,
    verify_and_report,
)


# ── platform 层：密钥发现 ────────────────────────────────────────────────


def test_list_ssh_keys_empty(monkeypatch, tmp_path):
    """~/.ssh 不存在 → 空列表。"""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert plat.list_ssh_keys() == []


def test_list_ssh_keys_finds_existing(monkeypatch, tmp_path):
    """~/.ssh 下有 id_ed25519 → 列出。"""
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "id_ed25519").write_text("PRIVATE")
    (ssh_dir / "id_ed25519.pub").write_text("PUBLIC")
    (ssh_dir / "known_hosts").write_text("")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    keys = plat.list_ssh_keys()
    assert keys == ["id_ed25519"]


def test_default_key_path_skips_existing(monkeypatch, tmp_path):
    """已有 id_ed25519 → 默认路径给 id_ecdsa。"""
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "id_ed25519").write_text("PRIVATE")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    p = plat.default_key_path()
    assert p.name == "id_ecdsa"


def test_public_key_path_appends_pub():
    assert plat.public_key_path(Path("~/.ssh/id_ed25519")).name == "id_ed25519.pub"


# ── platform 层：test_ssh_connection ─────────────────────────────────────


def test_test_ssh_connection_no_ssh(monkeypatch):
    monkeypatch.setattr(plat, "find_executable", lambda name: None)
    ok, msg = plat.test_ssh_connection("ubuntu@10.0.0.9")
    assert ok is False
    assert "找不到 ssh" in msg


def test_test_ssh_connection_ok(monkeypatch):
    """ssh 返回 0 → ok=True。"""
    monkeypatch.setattr(plat, "find_executable", lambda name: "/usr/bin/ssh" if name == "ssh" else None)

    def fake_run(argv, *, timeout=60.0, env=None):
        return 0, "", ""

    monkeypatch.setattr(plat, "run", fake_run)
    ok, msg = plat.test_ssh_connection("ubuntu@10.0.0.9")
    assert ok is True
    assert "免密" in msg


def test_test_ssh_connection_permission_denied(monkeypatch):
    """ssh 返回 255 + Permission denied → 公钥认证失败提示。"""
    monkeypatch.setattr(plat, "find_executable", lambda name: "/usr/bin/ssh" if name == "ssh" else None)

    def fake_run(argv, *, timeout=60.0, env=None):
        return 255, "", "Permission denied (publickey)."

    monkeypatch.setattr(plat, "run", fake_run)
    ok, msg = plat.test_ssh_connection("ubuntu@10.0.0.9")
    assert ok is False
    assert "公钥认证失败" in msg
    assert "ssh-setup" in msg


def test_test_ssh_connection_host_key_verification(monkeypatch):
    """ssh 返回 host key verification failed → 提示先手动 ssh 接受指纹。"""
    monkeypatch.setattr(plat, "find_executable", lambda name: "/usr/bin/ssh" if name == "ssh" else None)

    def fake_run(argv, *, timeout=60.0, env=None):
        return 255, "", "Host key verification failed."

    monkeypatch.setattr(plat, "run", fake_run)
    ok, msg = plat.test_ssh_connection("ubuntu@10.0.0.9")
    assert ok is False
    assert "主机指纹" in msg


def test_test_ssh_connection_timeout(monkeypatch):
    """ssh 超时 → 超时提示。"""
    monkeypatch.setattr(plat, "find_executable", lambda name: "/usr/bin/ssh" if name == "ssh" else None)

    def fake_run(argv, *, timeout=60.0, env=None):
        raise TimeoutError("timeout")

    monkeypatch.setattr(plat, "run", fake_run)
    ok, msg = plat.test_ssh_connection("ubuntu@10.0.0.9")
    assert ok is False
    assert "超时" in msg


# ── ssh_setup 层：ensure_key ─────────────────────────────────────────────


def test_ensure_key_reuses_existing(monkeypatch, tmp_path):
    """已有密钥 → 复用，不生成。"""
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    key = ssh_dir / "id_ed25519"
    key.write_text("EXISTING")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    path, status = ensure_key()
    assert path == key
    assert "已有" in status


def test_ensure_key_generates_new(monkeypatch, tmp_path):
    """没有密钥 → 调 ssh-keygen 生成。"""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(plat, "find_executable",
                        lambda name: "/usr/bin/ssh-keygen" if name == "ssh-keygen" else "/usr/bin/ssh")

    def fake_keygen(key_path, *, comment, key_type="ed25519", env=None):
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_text("NEW-PRIVATE")
        key_path.with_suffix(".pub").write_text("NEW-PUBLIC")
        return 0, "ok", ""

    monkeypatch.setattr(plat, "ssh_keygen", fake_keygen)
    path, status = ensure_key(comment="test@host")
    assert path.name == "id_ed25519"
    assert path.read_text() == "NEW-PRIVATE"
    assert "已生成" in status


def test_ensure_key_no_ssh_keygen(monkeypatch, tmp_path):
    """没有 ssh-keygen → 报错。"""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(plat, "find_executable", lambda name: None)
    with pytest.raises(SshSetupError) as exc:
        ensure_key()
    assert "ssh-keygen" in str(exc.value)


# ── ssh_setup 层：push_public_key ────────────────────────────────────────


def test_push_public_key_missing_pub(monkeypatch, tmp_path):
    """私钥存在但公钥文件缺失 → 报错。"""
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    key = ssh_dir / "id_ed25519"
    key.write_text("PRIVATE")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(plat, "find_executable", lambda name: "/usr/bin/ssh")
    with pytest.raises(SshSetupError) as exc:
        push_public_key("ubuntu@10.0.0.9", key)
    assert "公钥" in str(exc.value)


def test_push_public_key_via_copy_id(monkeypatch, tmp_path):
    """有 ssh-copy-id → 走 copy-id 路径。"""
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    key = ssh_dir / "id_ed25519"
    key.write_text("PRIVATE")
    (ssh_dir / "id_ed25519.pub").write_text("ssh-ed25519 AAAA test@host")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(plat, "find_executable",
                        lambda name: {"ssh": "/usr/bin/ssh",
                                      "ssh-copy-id": "/usr/bin/ssh-copy-id"}.get(name))

    class FakeProc:
        returncode = 0

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeProc())
    msg = push_public_key("ubuntu@10.0.0.9", key)
    assert "ssh-copy-id" in msg


def test_push_public_key_copy_id_fails(monkeypatch, tmp_path):
    """ssh-copy-id 返回非零 → 报错。"""
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    key = ssh_dir / "id_ed25519"
    key.write_text("PRIVATE")
    (ssh_dir / "id_ed25519.pub").write_text("ssh-ed25519 AAAA test@host")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(plat, "find_executable",
                        lambda name: {"ssh": "/usr/bin/ssh",
                                      "ssh-copy-id": "/usr/bin/ssh-copy-id"}.get(name))

    class FakeProc:
        returncode = 1

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeProc())
    with pytest.raises(SshSetupError) as exc:
        push_public_key("ubuntu@10.0.0.9", key)
    assert "ssh-copy-id 失败" in str(exc.value)


def test_push_public_key_manual_fallback(monkeypatch, tmp_path):
    """Windows 没有 ssh-copy-id → 手动推。"""
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    key = ssh_dir / "id_ed25519"
    key.write_text("PRIVATE")
    (ssh_dir / "id_ed25519.pub").write_text("ssh-ed25519 AAAA test@host")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # 只给 ssh，不给 ssh-copy-id
    monkeypatch.setattr(plat, "find_executable",
                        lambda name: "/usr/bin/ssh" if name == "ssh" else None)

    class FakeProc:
        returncode = 0
        stdout = b""
        stderr = b""

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeProc())
    msg = push_public_key("ubuntu@10.0.0.9", key)
    assert "手动" in msg


def test_push_public_key_manual_fails(monkeypatch, tmp_path):
    """手动推送返回非零 → 报错。"""
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    key = ssh_dir / "id_ed25519"
    key.write_text("PRIVATE")
    (ssh_dir / "id_ed25519.pub").write_text("ssh-ed25519 AAAA test@host")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(plat, "find_executable",
                        lambda name: "/usr/bin/ssh" if name == "ssh" else None)

    class FakeProc:
        returncode = 1
        stdout = b""
        stderr = b"Permission denied"

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeProc())
    with pytest.raises(SshSetupError) as exc:
        push_public_key("ubuntu@10.0.0.9", key)
    assert "推送公钥失败" in str(exc.value)


def test_push_public_key_invalid_target(monkeypatch, tmp_path):
    """target 不是 user@host → validate_target 报错。"""
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    key = ssh_dir / "id_ed25519"
    key.write_text("PRIVATE")
    (ssh_dir / "id_ed25519.pub").write_text("ssh-ed25519 AAAA test@host")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(plat, "find_executable", lambda name: "/usr/bin/ssh")
    with pytest.raises(Exception) as exc:
        push_public_key("not-a-target", key)
    assert "user@host" in str(exc.value)


# ── CLI 层：ssh-check / ssh-setup ────────────────────────────────────────


def run_cli(capsys, argv):
    code = main(argv)
    out = capsys.readouterr().out
    return code, out


def test_cli_ssh_check_no_keys(monkeypatch, tmp_path, capsys):
    """本机无密钥 → 报错并引导 ssh-setup。"""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(plat, "find_executable", lambda name: "/usr/bin/ssh" if name == "ssh" else None)
    code, out = run_cli(capsys, ["ssh-check", "ubuntu@10.0.0.9"])
    assert code == 7  # EXIT_REFUSED
    assert "没有 SSH 私钥" in out
    assert "ssh-setup" in out


def test_cli_ssh_check_ok(monkeypatch, tmp_path, capsys):
    """有密钥 + 免密通 → 退出码 0。"""
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "id_ed25519").write_text("PRIVATE")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(plat, "find_executable", lambda name: "/usr/bin/ssh" if name == "ssh" else None)
    monkeypatch.setattr(plat, "test_ssh_connection",
                        lambda target, **kw: (True, "免密登录已配置。"))
    code, out = run_cli(capsys, ["ssh-check", "ubuntu@10.0.0.9"])
    assert code == 0
    assert "免密" in out


def test_cli_ssh_check_fail(monkeypatch, tmp_path, capsys):
    """有密钥但免密不通 → 退出码 7。"""
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "id_ed25519").write_text("PRIVATE")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(plat, "find_executable", lambda name: "/usr/bin/ssh" if name == "ssh" else None)
    monkeypatch.setattr(plat, "test_ssh_connection",
                        lambda target, **kw: (False, "公钥认证失败。"))
    code, out = run_cli(capsys, ["ssh-check", "ubuntu@10.0.0.9"])
    assert code == 7
    assert "公钥认证失败" in out


def test_cli_ssh_check_bad_target(monkeypatch, tmp_path, capsys):
    """target 不是 user@host → 退出码 2。"""
    monkeypatch.setattr(plat, "find_executable", lambda name: "/usr/bin/ssh")
    code, out = run_cli(capsys, ["ssh-check", "bad-target"])
    assert code == 2
    assert "user@host" in out


def test_cli_ssh_check_no_ssh(monkeypatch, capsys):
    """没有 ssh 客户端 → 退出码 6。"""
    monkeypatch.setattr(plat, "find_executable", lambda name: None)
    code, out = run_cli(capsys, ["ssh-check", "ubuntu@10.0.0.9"])
    assert code == 6
    assert "ssh" in out


def test_cli_ssh_check_json(monkeypatch, tmp_path, capsys):
    """--json 输出结构正确。"""
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "id_ed25519").write_text("PRIVATE")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(plat, "find_executable", lambda name: "/usr/bin/ssh" if name == "ssh" else None)
    monkeypatch.setattr(plat, "test_ssh_connection",
                        lambda target, **kw: (True, "免密登录已配置。"))
    code, out = run_cli(capsys, ["ssh-check", "ubuntu@10.0.0.9", "--json"])
    import json
    payload = json.loads(out)
    assert payload["data"]["ok"] is True
    assert "id_ed25519" in payload["data"]["ssh_keys"]


def test_cli_ssh_setup_success(monkeypatch, tmp_path, capsys):
    """完整流程：生成密钥 → 推公钥 → 验证通过。"""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(plat, "find_executable",
                        lambda name: {"ssh": "/usr/bin/ssh",
                                      "ssh-keygen": "/usr/bin/ssh-keygen"}.get(name))

    def fake_keygen(key_path, *, comment, key_type="ed25519", env=None):
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_text("NEW-PRIVATE")
        key_path.with_suffix(".pub").write_text("ssh-ed25519 AAAA " + comment)
        return 0, "ok", ""

    monkeypatch.setattr(plat, "ssh_keygen", fake_keygen)

    # push_public_key 用 copy-id 路径
    class FakeProc:
        returncode = 0

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeProc())
    # 因为 find_executable 给了 ssh-copy-id，push_public_key 会走 copy-id
    # 但 copy-id 检查的是 plat.find_executable("ssh-copy-id")，我们上面只给了 ssh 和 ssh-keygen
    # 修正：给 ssh-copy-id 也返回路径
    monkeypatch.setattr(plat, "find_executable",
                        lambda name: {"ssh": "/usr/bin/ssh",
                                      "ssh-keygen": "/usr/bin/ssh-keygen",
                                      "ssh-copy-id": "/usr/bin/ssh-copy-id"}.get(name))

    # 验证通过
    monkeypatch.setattr(plat, "test_ssh_connection",
                        lambda target, **kw: (True, "免密登录已配置。"))
    code, out = run_cli(capsys, ["ssh-setup", "ubuntu@10.0.0.9"])
    assert code == 0
    assert "已生成" in out
    assert "ssh-copy-id" in out or "手动" in out
    assert "免密" in out
    assert "migrate server export" in out


def test_cli_ssh_setup_verify_fails(monkeypatch, tmp_path, capsys):
    """推公钥成功但验证失败 → 退出码 7。"""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(plat, "find_executable",
                        lambda name: {"ssh": "/usr/bin/ssh",
                                      "ssh-keygen": "/usr/bin/ssh-keygen",
                                      "ssh-copy-id": "/usr/bin/ssh-copy-id"}.get(name))

    def fake_keygen(key_path, *, comment, key_type="ed25519", env=None):
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_text("NEW-PRIVATE")
        key_path.with_suffix(".pub").write_text("ssh-ed25519 AAAA " + comment)
        return 0, "ok", ""

    monkeypatch.setattr(plat, "ssh_keygen", fake_keygen)

    class FakeProc:
        returncode = 0

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeProc())
    # 验证失败
    monkeypatch.setattr(plat, "test_ssh_connection",
                        lambda target, **kw: (False, "公钥认证失败。"))
    code, out = run_cli(capsys, ["ssh-setup", "ubuntu@10.0.0.9"])
    assert code == 7
    assert "公钥已推但验证失败" in out


def test_cli_ssh_setup_no_ssh(monkeypatch, capsys):
    """没有 ssh 客户端 → 退出码 6。"""
    monkeypatch.setattr(plat, "find_executable", lambda name: None)
    code, out = run_cli(capsys, ["ssh-setup", "ubuntu@10.0.0.9"])
    assert code == 6


def test_cli_ssh_setup_json(monkeypatch, tmp_path, capsys):
    """--json 输出结构正确。"""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(plat, "find_executable",
                        lambda name: {"ssh": "/usr/bin/ssh",
                                      "ssh-keygen": "/usr/bin/ssh-keygen",
                                      "ssh-copy-id": "/usr/bin/ssh-copy-id"}.get(name))

    def fake_keygen(key_path, *, comment, key_type="ed25519", env=None):
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_text("NEW-PRIVATE")
        key_path.with_suffix(".pub").write_text("ssh-ed25519 AAAA " + comment)
        return 0, "ok", ""

    monkeypatch.setattr(plat, "ssh_keygen", fake_keygen)

    class FakeProc:
        returncode = 0

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: FakeProc())
    monkeypatch.setattr(plat, "test_ssh_connection",
                        lambda target, **kw: (True, "免密登录已配置。"))
    code, out = run_cli(capsys, ["ssh-setup", "ubuntu@10.0.0.9", "--json"])
    import json
    payload = json.loads(out)
    assert payload["data"]["ok"] is True
    assert "key" in payload["data"]


# ── doctor 集成：SSH 能力检查 ────────────────────────────────────────────


def test_doctor_includes_ssh(monkeypatch, tmp_path, capsys):
    """doctor 输出包含 ssh / ssh_keys 字段。"""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(plat, "find_executable",
                        lambda name: {"ssh": "/usr/bin/ssh",
                                      "git": "/usr/bin/git"}.get(name))
    code, out = run_cli(capsys, ["doctor"])
    assert code == 0
    assert "ssh" in out
    # 无密钥时应提示
    assert "ssh-setup" in out or "ssh：" in out
