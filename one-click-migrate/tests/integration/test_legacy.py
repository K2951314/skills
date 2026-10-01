"""legacy 旧包导入测试：现场按旧参数生成 openssl 包，再走只读导入路径。

缺 openssl / Git Bash 时整体跳过（引擎主路径不依赖它们）。
"""

from __future__ import annotations

import io
import subprocess
import tarfile
from pathlib import Path

import pytest

from migrate_engine import platform as plat
from migrate_engine.unpack import PackageError, open_package, restore_package

PW = "legacy-passphrase-123"


def _have_openssl() -> bool:
    if plat.find_executable("openssl"):
        return True
    bash = plat.find_executable("bash")
    if not bash:
        return False
    rc, _out, _err = plat.run_binary([bash, "-lc", "command -v openssl"])
    return rc == 0


def _make_legacy_package(tmp_path: Path, files: dict[str, bytes], passphrase: str) -> Path:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in files.items():
            payload = data if isinstance(data, bytes) else data.encode("utf-8")
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
    plain = buf.getvalue()

    out = tmp_path / "legacy.tar.gz.enc"
    openssl = plat.find_executable("openssl")
    if openssl:
        proc = subprocess.run(
            [openssl, "enc", "-aes-256-cbc", "-salt", "-pbkdf2", "-iter", "200000",
             "-pass", "pass:" + passphrase, "-out", str(out)],
            input=plain, capture_output=True,
        )
    else:
        bash = plat.find_executable("bash")
        script = (f"openssl enc -aes-256-cbc -salt -pbkdf2 -iter 200000 "
                  f"-pass pass:{passphrase} -out \"$1\"")
        # 脚本体不含用户数据之外的内容；口令仅出现在这条一次性命令里
        proc = subprocess.run([bash, "-lc", script, "bash", str(out)],
                              input=plain, capture_output=True)
    assert proc.returncode == 0, proc.stderr
    return out


pytestmark = pytest.mark.skipif(not _have_openssl(), reason="没有 openssl/Git Bash")


def test_legacy_workspace_roundtrip(tmp_path):
    pkg = _make_legacy_package(tmp_path, {
        ".env": "JWT_SECRET=legacy-secret\n",
        "keys/license_public.pem": "-----BEGIN PUBLIC KEY-----\n",
        "quotation.db": b"fake-sqlite-bytes",
    }, PW)
    info, files = open_package(pkg, PW)
    assert info["legacy"] is True
    assert info["kind"] == "workspace"
    assert set(files) == {".env", "keys/license_public.pem", "quotation.db"}

    target = tmp_path / "newpc"
    target.mkdir()
    _info, plan, report = restore_package(pkg, PW, target)
    assert ".env" in report.written
    assert (target / ".env").read_text(encoding="utf-8") == "JWT_SECRET=legacy-secret\n"


def test_legacy_server_kind_by_marker(tmp_path):
    pkg = _make_legacy_package(tmp_path, {
        "sqdb.dump": b"PGDMP-fake",
        "etc-sq.env": "JWT_SECRET=only-on-server\n",
    }, PW)
    info, files = open_package(pkg, PW)
    assert info["kind"] == "server"
    with pytest.raises(PackageError) as exc:
        restore_package(pkg, PW, tmp_path / "x")
    assert exc.value.exit_code == 7


def test_legacy_both_markers_fails_safe_to_server(tmp_path):
    pkg = _make_legacy_package(tmp_path, {
        ".env": "A=1\n",
        "sqdb.dump": b"PGDMP-fake",
        "etc-sq.env": "B=2\n",
    }, PW)
    info, _files = open_package(pkg, PW)
    assert info["kind"] == "server"


def test_legacy_dot_slash_members_are_readable(tmp_path):
    """`tar -C <dir> .` 打出的旧包，成员名必须能被读（A17）。

    pull_from_server.sh 就是这样打包的：每个成员名都带 `./` 前缀。
    成员预检原本无条件拒绝 `.` 段，于是格式完全合法的 server 包被当成
    「包内成员不安全」拒掉——而那是重建服务器唯一依据的包。
    """
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "etc-sq.env").write_text("JWT_SECRET=on-server\n", encoding="utf-8")
    (stage / "sqdb.dump").write_bytes(b"PGDMP-fake")

    plain = subprocess.run(
        ["tar", "-czf", "-", "-C", str(stage), "."],
        capture_output=True, check=True).stdout
    # 确认这批成员名真的带 ./（`tar -C dir .` 还会多一个 "." 目录条目，
    # 引擎按 isfile() 过滤掉它，所以这里只管文件成员）
    with tarfile.open(fileobj=io.BytesIO(plain), mode="r:gz") as tf:
        names = [n for n in tf.getnames() if n != "."]
    assert names and all(n.startswith("./") for n in names), names

    pkg = tmp_path / "dot-slash.enc"
    openssl = plat.find_executable("openssl")
    if openssl:
        proc = subprocess.run(
            [openssl, "enc", "-aes-256-cbc", "-salt", "-pbkdf2", "-iter", "200000",
             "-pass", "pass:" + PW, "-out", str(pkg)],
            input=plain, capture_output=True)
        assert proc.returncode == 0, proc.stderr
    else:
        bash = plat.find_executable("bash")
        script = ("openssl enc -aes-256-cbc -salt -pbkdf2 -iter 200000 "
                  f"-pass pass:{PW} -out \"$1\"")
        proc = subprocess.run([bash, "-lc", script, "bash", str(pkg)],
                              input=plain, capture_output=True)
        assert proc.returncode == 0, proc.stderr

    info, files = open_package(pkg, PW)
    # 归一化后不带 ./ 前缀，且判型为 server
    assert info["kind"] == "server"
    assert set(files) == {"etc-sq.env", "sqdb.dump"}
    assert not any(n.startswith("./") for n in files)

    # server 包仍然拒绝合并进项目工作区
    with pytest.raises(PackageError) as exc:
        restore_package(pkg, PW, tmp_path / "x")
    assert exc.value.exit_code == 7


def test_legacy_wrong_passphrase(tmp_path):
    pkg = _make_legacy_package(tmp_path, {".env": "A=1\n"}, PW)
    with pytest.raises(Exception):
        open_package(pkg, "totally-wrong-passphrase")


def test_legacy_traversal_member_refused(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name="../evil.txt")
        data = b"x"
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    plain = buf.getvalue()
    out = tmp_path / "evil.enc"
    openssl = plat.find_executable("openssl")
    if openssl:
        subprocess.run([openssl, "enc", "-aes-256-cbc", "-salt", "-pbkdf2",
                        "-iter", "200000", "-pass", "pass:" + PW, "-out", str(out)],
                       input=plain, capture_output=True, check=True)
    else:
        bash = plat.find_executable("bash")
        subprocess.run([bash, "-lc",
                        f"openssl enc -aes-256-cbc -salt -pbkdf2 -iter 200000 "
                        f"-pass pass:{PW} -out \"$1\"", "bash", str(out)],
                       input=plain, capture_output=True, check=True)
    with pytest.raises(PackageError) as exc:
        open_package(out, PW)
    assert "不安全" in str(exc.value)
