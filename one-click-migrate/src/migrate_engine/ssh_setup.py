"""SSH 免密登录配置：生成密钥 + 推公钥到服务器。

换机后新机器没有 SSH 密钥，``server export`` 用 ``BatchMode=yes`` 会直接
失败。这个模块把「生成密钥 → 推公钥 → 验证连接」串成一条线，让用户
跑一条命令就能把免密配好。

安全约束：
- 推公钥用 ``ssh-copy-id``（POSIX）或手动 ``cat pubkey | ssh target '...'``（Windows
  没有 ssh-copy-id 时的 fallback）。这一步**需要交互输服务器密码**——是
  唯一一次需要密码，之后就免密了。
- 不自动接受新主机指纹：先手动 ``ssh target`` 一次接受指纹，或用
  ``StrictHostKeyChecking=accept-new`` 只在本模块的推送命令里用（不写进
  全局 SSH 配置）。
- 私钥 passphrase 用空字符串——服务器场景的密钥要无人值守，加了 passphrase
  每次 BatchMode=yes 都会失败。
"""

from __future__ import annotations

import shlex
from pathlib import Path

from . import EXIT_ERROR, EXIT_PLATFORM, EXIT_REFUSED
from . import platform as plat
from .server import ServerError, validate_target


class SshSetupError(Exception):
    def __init__(self, message: str, *, exit_code: int = EXIT_ERROR):
        super().__init__(message)
        self.exit_code = exit_code


def ensure_key(*, comment: str | None = None, force: bool = False
               ) -> tuple[Path, str]:
    """确保本机有 SSH 私钥。返回 (私钥路径, 状态描述)。

    - 已有密钥：直接用，除非 force=True
    - 没有：生成 ed25519（比 RSA 短、快、安全）

    comment 默认 ``user@hostname``，方便服务器上认出来是谁的密钥。
    """
    ssh_d = plat.ssh_dir()
    # 先看已有密钥——list_ssh_keys 按优先级返回，第一个就是该用的
    if not force:
        existing = plat.list_ssh_keys()
        if existing:
            return ssh_d / existing[0], f"已有密钥：{existing[0]}"

    # 没有已有密钥（或 force=True 要新建）：用 default_key_path 找可用名字
    key_path = plat.default_key_path()
    if key_path.exists() and force:
        # force 只覆盖生成，不删旧密钥——删私钥是不可逆操作，让用户自己删
        # 找下一个可用的名字
        for name in plat.SSH_KEY_NAMES:
            p = ssh_d / name
            if not p.exists():
                key_path = p
                break
        else:
            raise SshSetupError(
                f"~/.ssh 下已有全部常见密钥（{', '.join(plat.SSH_KEY_NAMES)}），"
                "用 --force 会在旁边新建，但当前没有可用名字。"
                "手动指定路径：ssh-keygen -t ed25519 -f ~/.ssh/custom-name"
            )

    if plat.find_executable("ssh-keygen") is None:
        raise SshSetupError(
            "找不到 ssh-keygen。Windows 上需在「设置 → 应用 → 可选功能」"
            "装 OpenSSH 客户端；POSIX 上 openssh-client 包。",
            exit_code=EXIT_PLATFORM,
        )

    import getpass
    import socket

    comment = comment or f"{getpass.getuser()}@{socket.gethostname()}"
    rc, out, err = plat.ssh_keygen(key_path, comment=comment)
    if rc != 0:
        raise SshSetupError(
            f"ssh-keygen 失败（rc={rc}）：{err or out}",
        )
    return key_path, f"已生成 {key_path.name}（{comment}）"


def push_public_key(target: str, key_path: Path, *,
                    port: int = 22,
                    accept_new_host: bool = True) -> str:
    """把公钥推到 target 的 ``~/.ssh/authorized_keys``。

    这一步**需要交互输服务器密码**——是唯一一次。之后就免密了。

    accept_new_host=True 时，用 ``StrictHostKeyChecking=accept-new`` 自动
    接受新主机指纹（只对本命令生效，不写进全局配置）。False 时用
    ``StrictHostKeyChecking=yes``——遇新主机直接失败，要求用户先手动 ssh 一次。
    """
    target = validate_target(target)
    pub_path = plat.public_key_path(key_path)
    if not pub_path.is_file():
        raise SshSetupError(
            f"找不到公钥文件：{pub_path}。"
            f"私钥 {key_path} 似乎不完整，手动跑 `ssh-keygen -y -f {key_path} > {pub_path}` 补。",
        )

    ssh = plat.find_executable("ssh")
    if ssh is None:
        raise SshSetupError(
            "找不到 ssh 客户端。",
            exit_code=EXIT_PLATFORM,
        )

    # 优先 ssh-copy-id（POSIX 有，Windows 没有）
    if plat.find_executable("ssh-copy-id"):
        return _push_via_copy_id(target, pub_path, port=port,
                                 accept_new_host=accept_new_host)
    # Windows fallback：手动 cat pubkey | ssh target 'mkdir + cat >> authorized_keys'
    return _push_via_manual(target, pub_path, port=port,
                            accept_new_host=accept_new_host)


def _push_via_copy_id(target: str, pub_path: Path, *, port: int,
                      accept_new_host: bool) -> str:
    """用 ssh-copy-id 推公钥（POSIX 路径）。"""
    argv = [
        "ssh-copy-id",
        "-p", str(port),
        "-i", str(pub_path),
    ]
    if accept_new_host:
        # ssh-copy-id 透传给 ssh 的选项
        argv.extend(["-o", "StrictHostKeyChecking=accept-new"])
    argv.append(target)
    # ssh-copy-id 会交互要密码——不能用 run_binary（capture_output 会吞掉 prompt）
    # 这里用 subprocess.run 不捕获 stdin，让它直接继承终端
    import subprocess
    import sys

    try:
        proc = subprocess.run(argv, stdin=sys.stdin, stdout=sys.stdout,
                              stderr=sys.stderr)
    except FileNotFoundError as exc:
        raise SshSetupError(f"ssh-copy-id 不可用：{exc}", exit_code=EXIT_PLATFORM)
    if proc.returncode != 0:
        raise SshSetupError(
            f"ssh-copy-id 失败（rc={proc.returncode}）。"
            "常见原因：服务器密码输错、用户名不对、服务器禁了密码登录。"
        )
    return f"已通过 ssh-copy-id 推送公钥到 {target}"


def _push_via_manual(target: str, pub_path: Path, *, port: int,
                     accept_new_host: bool) -> str:
    """Windows 没有 ssh-copy-id 时的 fallback：手动推。

    命令体：把公钥经 stdin 传给 ssh，服务器端 mkdir + append。
    这一步会交互要服务器密码。
    """
    import subprocess
    import sys

    pub_data = pub_path.read_text(encoding="utf-8").strip()
    # 远端命令：确保 ~/.ssh 存在且权限对，追加公钥
    remote_cmd = (
        "umask 077; "
        "mkdir -p ~/.ssh; "
        "touch ~/.ssh/authorized_keys; "
        f"echo {shlex.quote(pub_data)} >> ~/.ssh/authorized_keys; "
        "chmod 700 ~/.ssh; "
        "chmod 600 ~/.ssh/authorized_keys"
    )

    ssh = plat.find_executable("ssh")
    ssh_opts = ["-p", str(port)]
    if accept_new_host:
        ssh_opts.extend(["-o", "StrictHostKeyChecking=accept-new"])
    else:
        ssh_opts.extend(["-o", "StrictHostKeyChecking=yes"])
    ssh_opts.extend(["-o", "ConnectTimeout=10"])

    argv = [ssh, *ssh_opts, target, remote_cmd]
    try:
        proc = subprocess.run(argv, input=pub_data.encode("utf-8") + b"\n",
                              stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)
    except FileNotFoundError as exc:
        raise SshSetupError(f"ssh 不可用：{exc}", exit_code=EXIT_PLATFORM)
    if proc.returncode != 0:
        err = plat.decode_output(proc.stderr).strip()
        raise SshSetupError(
            f"推送公钥失败（rc={proc.returncode}）：{err}\n"
            "常见原因：服务器密码输错、用户名不对、服务器禁了密码登录。"
        )
    return f"已手动推送公钥到 {target}:~/.ssh/authorized_keys"


def verify_and_report(target: str) -> tuple[bool, str]:
    """验证免密登录，返回 (ok, 人可读信息)。"""
    return plat.test_ssh_connection(target)
