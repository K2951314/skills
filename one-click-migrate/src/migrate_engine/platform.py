"""平台适配层：子进程调用、输出解码、git 探针、能力体检。

设计约束（踩过的坑，勿回退）：
- 调外部工具一律参数数组（shell=False）。路径含中文/空格不会被二次解析。
- 子进程输出按 bytes 收再按平台解码；禁 text=True —— 它会把命令的
  真实错误盖成 UnicodeDecodeError（智能询价 2026-09 实测）。
- Windows 上命令输出可能是 GBK；Linux/macOS 是 UTF-8。解码失败再兜底 replace。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

#: 按平台排列的解码候选顺序。
_DECODE_CANDIDATES = ("utf-8", "gbk", "mbcs") if IS_WINDOWS else ("utf-8",)


def decode_output(raw: bytes) -> str:
    """把子进程输出解码成 str，按平台候选顺序尝试，最后兜底 replace。"""
    for enc in _DECODE_CANDIDATES:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def run(argv: list[str], *, timeout: float = 60.0, env: dict[str, str] | None = None,
        cwd: Path | str | None = None, input_bytes: bytes | None = None) -> tuple[int, str, str]:
    """跑一个外部命令，返回 (exit_code, stdout_text, stderr_text)。二进制输出用 run_binary。"""
    rc, out, err = run_binary(argv, timeout=timeout, env=env, cwd=cwd, input_bytes=input_bytes)
    return rc, decode_output(out), decode_output(err)


def run_binary(argv: list[str], *, timeout: float = 60.0, env: dict[str, str] | None = None,
               cwd: Path | str | None = None, input_bytes: bytes | None = None) -> tuple[int, bytes, bytes]:
    """跑一个外部命令，原样返回字节流。"""
    full_env = {**os.environ, **(env or {})}
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            input=input_bytes,
            timeout=timeout,
            env=full_env,
            cwd=str(cwd) if cwd else None,
        )
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"找不到可执行文件：{argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"命令超时（{timeout}s）：{argv[0]}") from exc
    return proc.returncode, proc.stdout, proc.stderr


def find_executable(name: str) -> str | None:
    return shutil.which(name)


# ── git 探针 ────────────────────────────────────────────────────────────


def git_repo_root(start: Path) -> Path | None:
    """从 start 向上找 .git；找不到返回 None（项目可以不在 git 里）。"""
    cur = start.resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / ".git").exists():
            return candidate
    return None


def _git(root: Path, args: list[str]) -> tuple[int, str]:
    rc, out, _err = run(["git", "-C", str(root), *args], timeout=30.0)
    if rc != 0:
        return rc, ""
    return rc, out


def git_tracked_files(root: Path) -> set[str] | None:
    """git 跟踪的文件集合（正斜杠相对路径）。非 git 仓库或无 git 返回 None。"""
    if find_executable("git") is None:
        return None
    rc, out = _git(root, ["ls-files", "-z"])
    if rc != 0:
        return None
    return {name for name in out.split("\0") if name}


def git_ignored_files(root: Path) -> list[str] | None:
    """被忽略且真实存在的文件（正斜杠相对路径）。非 git 仓库或无 git 返回 None。

    用 ls-files --others --ignored --exclude-standard：它逐文件列出，
    而不是 git status 那样把整个目录折叠成一行；--exclude-standard 覆盖
    .gitignore、.git/info/exclude 与全局 core.excludesFile。
    """
    if find_executable("git") is None:
        return None
    rc, out = _git(root, ["ls-files", "--others", "--ignored", "--exclude-standard", "-z"])
    if rc != 0:
        return None
    return sorted({name for name in out.split("\0") if name})


def git_ignore_check(root: Path, relpath: str) -> bool | None:
    """relpath（相对 root）是否被本仓库的忽略规则覆盖。

    返回 True=已忽略 / False=未忽略 / None=无法判断（非 git 仓库或无 git）。
    无法判断与「未忽略」必须区分——前者不该阻断，后者要。
    """
    if find_executable("git") is None:
        return None
    if git_repo_root(root) is None:
        return None
    rc, _out, _err = run(["git", "-C", str(root), "check-ignore", "-q", relpath])
    if rc == 0:
        return True
    if rc == 1:
        return False
    return None   # git 报错（配置损坏等）→ 无法判断


# ── 能力体检 ────────────────────────────────────────────────────────────


def doctor() -> dict:
    """能力探测报告。不打印任何密钥值。"""
    caps = {
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "python_ok": sys.version_info >= (3, 11),
        "git": find_executable("git"),
        "sqlite3": None,
        "tmp_writable": False,
    }
    try:
        conn = sqlite3.connect(":memory:")
        conn.close()
        caps["sqlite3"] = sqlite3.sqlite_version
    except sqlite3.Error:
        caps["sqlite3"] = None
    try:
        import tempfile

        with tempfile.NamedTemporaryFile(delete=True):
            caps["tmp_writable"] = True
    except OSError:
        caps["tmp_writable"] = False
    return caps
