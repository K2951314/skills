"""platform 层测试：解码、子进程调用、git 探针。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from migrate_engine import platform as plat


def test_decode_output_utf8():
    assert plat.decode_output("中文路径".encode("utf-8")) == "中文路径"


def test_decode_output_gbk_on_windows_or_fallback():
    raw = "中文路径".encode("gbk")
    out = plat.decode_output(raw)
    assert out  # 不抛异常；Windows 上应解出中文


def test_decode_output_replaces_garbage():
    out = plat.decode_output(b"\xff\xfe\x00bad")
    assert isinstance(out, str)


def test_run_uses_argv_array_and_captures():
    rc, out, err = plat.run([sys_python(), "-c", "print('ok-中文')"])
    assert rc == 0
    assert "ok-中文" in out


def test_run_missing_binary_raises():
    with pytest.raises(FileNotFoundError):
        plat.run(["definitely-not-a-real-binary-xyz"])


def test_run_binary_returns_raw_bytes():
    rc, out, _ = plat.run_binary([sys_python(), "-c", "import sys; sys.stdout.buffer.write(b'\\x00\\x01\\xff')"])
    assert rc == 0
    assert out == b"\x00\x01\xff"


def test_doctor_shape():
    caps = plat.doctor()
    assert caps["python_ok"] is True
    assert "platform" in caps
    assert caps["tmp_writable"] is True


def test_git_probes_on_repo(git_project):
    root = plat.git_repo_root(git_project)
    assert root == git_project.resolve()
    tracked = plat.git_tracked_files(git_project)
    assert tracked is not None
    assert ".gitignore" in tracked
    ignored = plat.git_ignored_files(git_project)
    assert ignored is not None
    assert ".env" in ignored
    assert "node_modules/leftpad.js" in ignored


def test_git_probes_outside_repo(tmp_path):
    assert plat.git_repo_root(tmp_path) is None


def sys_python() -> str:
    import sys

    return sys.executable
