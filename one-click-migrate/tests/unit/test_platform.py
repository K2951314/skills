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
    """仓库外 → 返回 None。

    ⚠ 不能直接拿 ``tmp_path`` 当「仓库外」：测试进程的 TEMP 可能被上层工具
    设到某个 Git 仓库里（某些 harness 会把 TEMP 指到项目内的 .tmp 目录，
    于是 pytest 的 tmp_path 天然就在那个仓库内）。这不是引擎的错——
    从仓库子目录向上找 .git，本来就该找到；错的是「tmp_path 一定在仓库外」
    这个测试前提。

    这里反过来：先找 tmp_path 的**第一个不带 .git 的祖先**，在那里现建一个
    子目录当隔离根。ancestor 上都没有 .git，新子目录自然也不会有。
    """
    probe = tmp_path.resolve()
    while plat.git_repo_root(probe) is not None and probe.parent != probe:
        probe = probe.parent
    clean_root = probe / "no-git-anywhere" / "deeper"
    clean_root.mkdir(parents=True, exist_ok=True)
    assert plat.git_repo_root(clean_root) is None


def sys_python() -> str:
    import sys

    return sys.executable
