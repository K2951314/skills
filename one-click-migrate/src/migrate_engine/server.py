"""服务器侧采集与上传：本机控制器通过 SSH 操作，服务器零安装。

安全约束：
- `ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10`：
  要密码、遇新主机指纹，立即停下交人工处理，绝不自动接受 known_hosts。
- 远端命令不拼字符串：本地生成脚本体，经 stdin 传给 `ssh target bash -s -- <参数>`，
  脚本里只解引用 $1 $2。库名、路径等用户输入只走位置参数。
- 拉到的明文只活在内存；打包即加密，落盘只有 .enc。
- pg_dump 输出校验 PGDMP 头；空 dump 一律失败。
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import EXIT_ERROR, EXIT_PLATFORM, EXIT_REFUSED, __version__
from .crypto import encrypt_blob
from .manifest import Item, Manifest
from .pack import KIND_SERVER, build_payload_zip
from . import platform as plat

#: user@host 形态。不accept URL、不accept 裸 IP 也行但建议 user@。
TARGET_RE = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9._-]+$")

SSH_OPTS = [
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=yes",
    "-o", "ConnectTimeout=10",
]


class ServerError(Exception):
    def __init__(self, message: str, *, exit_code: int = EXIT_ERROR):
        super().__init__(message)
        self.exit_code = exit_code


def validate_target(target: str) -> str:
    target = target.strip()
    if not TARGET_RE.match(target):
        raise ServerError(
            f"服务器地址必须是 user@host 形式（如 ubuntu@203.0.113.10）：{target!r}\n"
            "地址不写入仓库与 manifest；每次导出时当场确认。"
        )
    return target


def _ssh(target: str, script: str, *, args: list[str] | None = None,
         timeout: float = 120.0) -> tuple[int, bytes, str]:
    """ssh target bash -s -- <args>，脚本体走 stdin。返回 (rc, stdout_bytes, stderr_text)。"""
    if plat.find_executable("ssh") is None:
        raise ServerError("找不到 ssh 客户端。", exit_code=EXIT_PLATFORM)
    argv = ["ssh", *SSH_OPTS, target, "bash", "-s", "--", *(args or [])]
    rc, out, err = plat.run_binary(argv, timeout=timeout, input_bytes=script.encode("utf-8"))
    return rc, out, plat.decode_output(err).strip()


# ── 采集 ────────────────────────────────────────────────────────────────

_CAPTURE_FILE_SCRIPT = """#!/usr/bin/env bash
# $1 = 远端文件路径。优先无特权读，失败再 sudo -n（绝不交互）。
path="$1"
if [ -r "$path" ]; then
  cat -- "$path"
elif command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
  sudo -n cat -- "$path"
else
  echo "cannot read $path" >&2
  exit 3
fi
"""

_PG_DUMP_SCRIPT = """#!/usr/bin/env bash
# $1 = 数据库用户 $2 = 库名。sudo -n 非交互；失败即非零退出。
user="$1"; db="$2"
if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
  sudo -n -u "$user" pg_dump -F c --dbname="$db"
else
  pg_dump -F c --dbname="$db"
fi
"""

_METADATA_SCRIPT = """#!/usr/bin/env bash
echo "pulled_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
pg=$( (sudo -n -u postgres psql -t -A -c 'show server_version' 2>/dev/null || psql -t -A -c 'show server_version' 2>/dev/null) | tr -d '[:space:]')
echo "postgres=${pg:-unknown}"
py=$( (/opt/*/venv/bin/python --version 2>/dev/null || python3 --version 2>&1) | head -1 | tr -d '[:space:]')
echo "python=${py:-unknown}"
caddy=$(caddy version 2>/dev/null | head -1 | tr -d '[:space:]')
echo "caddy=${caddy:-not-installed}"
nginx=$(nginx -v 2>&1 | head -1 | tr -d '[:space:]')
echo "nginx=${nginx:-not-installed}"
"""


@dataclass
class CapturedFile:
    id: str
    name: str          # 包内名
    data: bytes
    cls: str
    sensitive: bool
    item_type: str


@dataclass
class ServerCaptureResult:
    entries: list[CapturedFile] = field(default_factory=list)
    metadata: str = ""
    notes: list[str] = field(default_factory=list)


def capture_server(target: str, manifest: Manifest, items: list[Item]) -> ServerCaptureResult:
    """从旧服务器采集声明过的资产，全部进内存。"""
    target = validate_target(target)
    result = ServerCaptureResult()

    for item in items:
        if item.scope not in {"server", "both"}:
            continue
        if item.capture == "pg_dump" or item.item_type == "pg":
            rc, out, err = _ssh(target, _PG_DUMP_SCRIPT,
                                args=[_item_field(item, "pg_user", "postgres"),
                                      _item_field(item, "pg_db", item.remote_path or "")],
                                timeout=600.0)
            if rc != 0 or not out:
                raise ServerError(
                    f"pg_dump 失败（{item.id}，rc={rc}）：{err or '空输出'}\n"
                    "确认数据库在跑、sudo 免密、库名正确。"
                )
            if not out.startswith(b"PGDMP"):
                raise ServerError(f"{item.id}：pg_dump 输出不是 PostgreSQL 自定义格式（缺 PGDMP 头）。")
            result.entries.append(CapturedFile(
                id=item.id, name=item.path, data=out, cls=item.cls,
                sensitive=True, item_type="pg"))
            result.notes.append(f"{item.id}：{_human(len(out))} 转储")
        else:
            remote_path = item.remote_path or item.path
            rc, out, err = _ssh(target, _CAPTURE_FILE_SCRIPT, args=[remote_path])
            if rc != 0:
                if item.on_missing == "block":
                    raise ServerError(f"读取远端文件失败（{item.id}：{remote_path}）：{err}")
                result.notes.append(f"{item.id}：读取失败，未进包（{err}）")
                continue
            if not out.strip():
                if item.on_missing == "block":
                    raise ServerError(f"远端文件为空（{item.id}：{remote_path}）。")
                result.notes.append(f"{item.id}：远端文件为空，未进包")
                continue
            name = item.path or remote_path.strip("/").replace("/", "-")
            result.entries.append(CapturedFile(
                id=item.id, name=name, data=out, cls=item.cls,
                sensitive=True, item_type="file"))

    rc, meta_bytes, err = _ssh(target, _METADATA_SCRIPT, timeout=60.0)
    if rc == 0:
        result.metadata = plat.decode_output(meta_bytes)
    else:
        result.metadata = f"pulled_at={datetime.utcnow().isoformat()}Z\n(版本探测失败：{err})\n"
        result.notes.append("版本锚点探测失败（不影响包，新机需人工对齐版本）")
    if not result.entries:
        raise ServerError("没有采集到任何服务器资产——检查 manifest 的 server 条目。")
    return result


def _item_field(item: Item, name: str, default: str) -> str:
    """取 item 上的 pg_user/pg_db 字段，缺失回退默认值。"""
    value = getattr(item, name, None)
    return value or default


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


# ── 打包 ────────────────────────────────────────────────────────────────


def build_server_package(result: ServerCaptureResult, manifest: Manifest,
                         passphrase: str, out_dir: Path, *,
                         stamp: str | None = None) -> Path:
    """把采集结果组成 server 包（与 workspace 同一容器格式）。"""
    import io
    import json
    import zipfile

    from .pack import PACKAGE_FORMAT

    stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{manifest.project}-server-{stamp}.enc"

    package_manifest = {
        "format": PACKAGE_FORMAT,
        "kind": KIND_SERVER,
        "project": manifest.project,
        "engine": __version__,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "hostname_hint": "server-package",
        "items": [
            {"id": e.id, "path": e.name, "bytes": len(e.data),
             "sha256": _sha(e.data), "class": e.cls, "sensitive": e.sensitive,
             "item_type": e.item_type}
            for e in result.entries
        ] + [{"id": "env-metadata", "path": "ENV-METADATA.txt",
              "bytes": len(result.metadata.encode("utf-8")),
              "sha256": _sha(result.metadata.encode("utf-8")),
              "class": "required", "sensitive": False, "item_type": "file"}],
        "rebuild": manifest.rebuild,
        "verify": manifest.verify,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(package_manifest, ensure_ascii=False, indent=2))
        for e in result.entries:
            zf.writestr(e.name, e.data)
        zf.writestr("ENV-METADATA.txt", result.metadata)
    out.write_bytes(encrypt_blob(buf.getvalue(), passphrase))
    return out


def _sha(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


# ── 上传（只进暂存目录；系统路径由人工执行） ──────────────────────────────


def upload_stage(package: Path, passphrase: str, target: str, *,
                 stage_dir: str = "/tmp/oc-migrate-stage",
                 hints: dict[str, dict] | None = None) -> list[str]:
    """解密 → 本地组 tar → 经 ssh stdin 落到目标机暂存目录。

    hints：{包内名: {"remote_path": ..., "pg_db": ...}}，来自本机 manifest 的
    server 条目，用于生成「人工待执行命令」的准确目标路径。

    返回建议人工执行的命令清单（pg_restore / sudo cp 等）。引擎不自行提权。
    """
    from .unpack import open_package

    target = validate_target(target)
    package_manifest, files = open_package(package, passphrase)
    if package_manifest["kind"] != KIND_SERVER:
        raise ServerError("只有 server 包能走上传通道。", exit_code=EXIT_REFUSED)

    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name in sorted(files):
            data = files[name][0]
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mtime = int(datetime.now().timestamp())
            tf.addfile(info, io.BytesIO(data))
    tar_bytes = buf.getvalue()

    rc, _out, err = _ssh_binary(target, f"mkdir -p {shlex.quote(stage_dir)} && tar -x -C {shlex.quote(stage_dir)}",
                                input_bytes=tar_bytes, timeout=300.0)
    if rc != 0:
        raise ServerError(f"上传到 {target}:{stage_dir} 失败：{err}")

    hints = hints or {}
    commands: list[str] = [f"# 在 {target} 上人工执行（引擎不自行提权；执行前先 diff）"]
    for record in package_manifest["items"]:
        name = record["path"]
        hint = hints.get(name, {})
        if record.get("item_type") == "pg":
            db = hint.get("pg_db") or "<新库名>"
            commands.append(f"sudo -u postgres pg_restore --no-owner --no-privileges -d {db} {stage_dir}/{name}")
        elif name == "ENV-METADATA.txt":
            continue  # 只是版本锚点，不用装
        else:
            remote = hint.get("remote_path") or name
            commands.append(f"sudo cp {stage_dir}/{name} {remote} && sudo chmod 640 {remote}")
    commands.append(f"rm -rf {stage_dir}   # 确认服务正常后清理明文暂存")
    return commands


def _ssh_binary(target: str, command: str, *, input_bytes: bytes,
                timeout: float) -> tuple[int, bytes, str]:
    if plat.find_executable("ssh") is None:
        raise ServerError("找不到 ssh 客户端。", exit_code=EXIT_PLATFORM)
    argv = ["ssh", *SSH_OPTS, target, command]
    rc, out, err = plat.run_binary(argv, timeout=timeout, input_bytes=input_bytes)
    return rc, out, plat.decode_output(err).strip()
