"""口令输入：交互不回显、EOF 当中止、环境变量/文件来源。

踩过的坑（智能询价 docs/machine-migration.md 坑 4）：Git Bash 里 getpass
会去开不存在的 /dev/tty，TERM 又带着 xterm 字样，于是它认定有 TTY 然后
死等——脚本看着像在等你输入，其实已经挂了。修法：stdin 不是管道时走
getpass，是管道/重定向时直接 readline；读到 EOF 一律返回 None（中止），
绝不当成空口令再问一遍（那会无限循环）。
"""

from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path

#: 新包口令最短长度。旧包导入不强制（legacy 口令可能更短）。
MIN_LENGTH = 12

ENV_VAR = "MIGRATE_PASSPHRASE"


class PassphraseAborted(Exception):
    """输入流结束或用户取消。"""


def _read_secret(prompt: str) -> str | None:
    if not os.isatty(0):
        # 管道 / 重定向：没有 TTY 可读，读 stdin 一行
        print(prompt, end="", file=sys.stderr, flush=True)
        line = sys.stdin.readline()
        if line == "":
            return None
        return line.rstrip("\r\n")
    try:
        return getpass.getpass(prompt)
    except EOFError:
        return None


def from_env() -> str | None:
    value = os.environ.get(ENV_VAR)
    return value or None


def from_file(path: str | Path) -> str | None:
    text = Path(path).read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line
    return None


def obtain(*, confirm: bool, source_file: str | None = None,
           enforce_min: bool = True) -> str:
    """取口令。confirm=True 用于导出（打错口令包永远解不开）。

    优先级：--passphrase-file > 环境变量 > 交互。
    交互输入两次并校验长度；EOF → PassphraseAborted。
    """
    if source_file is not None:
        value = from_file(source_file)
        if value is None:
            raise PassphraseAborted(f"口令文件是空的：{source_file}")
        _check(value, enforce_min)
        return value

    value = from_env()
    if value is not None:
        _check(value, enforce_min)
        return value

    while True:
        pw = _read_secret(f"迁移口令（至少 {MIN_LENGTH} 位，输入不显示）: ")
        if pw is None:
            raise PassphraseAborted("没有输入流，无法读取口令。请在终端里直接运行。")
        if enforce_min and len(pw) < MIN_LENGTH:
            print(f"口令至少 {MIN_LENGTH} 位。")
            continue
        if confirm:
            again = _read_secret("再输一次确认（口令丢了包永远解不开）: ")
            if again is None:
                raise PassphraseAborted("没有输入流，无法确认口令。")
            if pw != again:
                print("两次输入不一致，重来。")
                continue
        return pw


def _check(value: str, enforce_min: bool) -> None:
    if enforce_min and len(value) < MIN_LENGTH:
        raise PassphraseAborted(
            f"口令至少 {MIN_LENGTH} 位（来自 {ENV_VAR} 或口令文件）。"
        )
