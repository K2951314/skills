"""批量换机 CLI 测试：init / list / plan / export / verify / import。

用 --passphrase-file 避免交互。构造两个假项目 + 注册表，验证全链路。
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from migrate_engine.cli import main


PASSPHRASE = "test-passphrase-0123456789abcdef"


@pytest.fixture()
def passphrase_file(tmp_path: Path) -> Path:
    p = tmp_path / "pass.txt"
    p.write_text(PASSPHRASE + "\n", encoding="utf-8")
    return p


@pytest.fixture()
def two_projects(tmp_path: Path) -> tuple[Path, Path]:
    """两个带 manifest 的假项目，目录结构一样但 slug 不同。"""
    projects = []
    for slug in ["alpha", "beta"]:
        root = tmp_path / slug
        (root / ".migrate").mkdir(parents=True)
        (root / ".env").write_text(f"SECRET_{slug}=value123\nSQ_DEV=1\n", encoding="utf-8")
        (root / "config.local.json").write_text(f'{{"slug": "{slug}"}}', encoding="utf-8")
        (root / ".gitignore").write_text(
            ".env\nconfig.local.json\n.migrate/*\n*.enc\n", encoding="utf-8",
        )
        (root / ".migrate" / "manifest.toml").write_text(textwrap.dedent(f"""
            schema_version = 1
            project = "{slug}"
            artifacts_dir = ".migrate"

            [[items]]
            id = "env-file"
            path = ".env"
            class = "required"
            on_missing = "block"
            sensitive = true

            [[items]]
            id = "local-config"
            path = "config.local.json"
            class = "recommended"
        """), encoding="utf-8")
        projects.append(root)
    return projects[0], projects[1]


@pytest.fixture()
def registry_file(tmp_path: Path, two_projects) -> Path:
    alpha, beta = two_projects
    p = tmp_path / "registry.toml"
    p.write_text(textwrap.dedent(f"""
        schema_version = 1

        [[projects]]
        name = "alpha"
        path = "{alpha.as_posix()}"

        [[projects]]
        name = "beta"
        path = "{beta.as_posix()}"
    """), encoding="utf-8")
    return p


def run_cli(capsys, argv: list[str]) -> tuple[int, dict | None, str]:
    code = main(argv)
    out = capsys.readouterr().out
    payload = None
    if "--json" in argv:
        payload = json.loads(out) if out.strip() else None
    return code, payload, out


# ── batch init ───────────────────────────────────────────────────────────


def test_batch_init_print(tmp_path, capsys):
    code, _, out = run_cli(capsys, ["batch", "init", "--print"])
    assert code == 0
    assert "schema_version = 1" in out
    assert "[[projects]]" in out


def test_batch_init_writes_file(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    code, _, out = run_cli(capsys, ["batch", "init"])
    assert code == 0
    assert (tmp_path / ".migrate-registry.toml").exists()


def test_batch_init_conflict(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / ".migrate-registry.toml").write_text("schema_version=1", encoding="utf-8")
    code, _, out = run_cli(capsys, ["batch", "init"])
    assert code == 3  # EXIT_CONFLICTS
    assert "已存在" in out


# ── batch list ───────────────────────────────────────────────────────────


def test_batch_list(registry_file, capsys):
    code, payload, _ = run_cli(capsys, ["--json", "batch", "list",
                                         "--registry", str(registry_file)])
    assert code == 0
    assert payload["data"]["count"] == 2
    # Report.to_dict() 把 rows 放在 data["items"] 里
    names = [r["id"] for r in payload["data"]["items"]]
    assert "alpha" in names
    assert "beta" in names


def test_batch_list_missing_file(tmp_path, capsys):
    code, _, out = run_cli(capsys, ["batch", "list",
                                     "--registry", str(tmp_path / "nope.toml")])
    assert code == 2
    assert "没找到注册表" in out


# ── batch plan ───────────────────────────────────────────────────────────


def test_batch_plan(registry_file, capsys):
    code, payload, _ = run_cli(capsys, ["--json", "batch", "plan",
                                         "--registry", str(registry_file)])
    assert code == 0
    assert payload["data"]["project_count"] == 2
    results = payload["data"]["results"]
    assert len(results) == 2
    assert all(r["exit"] == 0 for r in results)


def test_batch_plan_only(registry_file, capsys):
    code, payload, _ = run_cli(capsys, ["--json", "batch", "plan",
                                         "--registry", str(registry_file),
                                         "--only", "alpha"])
    assert code == 0
    assert payload["data"]["project_count"] == 1
    assert payload["data"]["results"][0]["name"] == "alpha"


def test_batch_plan_exclude(registry_file, capsys):
    code, payload, _ = run_cli(capsys, ["--json", "batch", "plan",
                                         "--registry", str(registry_file),
                                         "--exclude", "beta"])
    assert code == 0
    names = [r["name"] for r in payload["data"]["results"]]
    assert "beta" not in names
    assert "alpha" in names


def test_batch_plan_only_unknown(registry_file, capsys):
    code, _, out = run_cli(capsys, ["batch", "plan",
                                     "--registry", str(registry_file),
                                     "--only", "nonexistent"])
    assert code == 2
    assert "没有的项目" in out


def test_batch_plan_missing_path(tmp_path, capsys):
    reg = tmp_path / "reg.toml"
    reg.write_text(textwrap.dedent("""
        schema_version = 1
        [[projects]]
        name = "ghost"
        path = "/nonexistent/path/xyz"
    """), encoding="utf-8")
    code, payload, _ = run_cli(capsys, ["--json", "batch", "plan",
                                         "--registry", str(reg)])
    assert code == 3  # 有阻断
    assert payload["data"]["results"][0]["exit"] != 0


# ── batch export / verify / import ───────────────────────────────────────


def test_batch_export_then_verify_then_import(registry_file, two_projects,
                                               passphrase_file, capsys, tmp_path):
    alpha, beta = two_projects

    # export
    code, payload, _ = run_cli(capsys, ["--json", "batch", "export",
                                         "--registry", str(registry_file),
                                         "--passphrase-file", str(passphrase_file)])
    assert code == 0, f"export failed: {payload}"
    results = payload["data"]["results"]
    assert len(results) == 2
    assert all(r["exit"] == 0 for r in results)
    # 每个项目都生成了 .enc
    for r in results:
        assert r["package"] is not None
        assert Path(r["package"]).exists()
        assert r["package"].endswith(".enc")

    # verify
    code, payload, _ = run_cli(capsys, ["--json", "batch", "verify",
                                         "--registry", str(registry_file),
                                         "--passphrase-file", str(passphrase_file)])
    assert code == 0, f"verify failed: {payload}"
    results = payload["data"]["results"]
    assert all(r["verified"] for r in results)

    # import：先删掉目标文件，模拟新机器
    (alpha / ".env").unlink()
    (alpha / "config.local.json").unlink()
    (beta / ".env").unlink()
    (beta / "config.local.json").unlink()

    code, payload, _ = run_cli(capsys, ["--json", "batch", "import",
                                         "--registry", str(registry_file),
                                         "--passphrase-file", str(passphrase_file)])
    assert code == 0, f"import failed: {payload}"
    results = payload["data"]["results"]
    assert all(r["imported"] for r in results)

    # 验证文件确实回来了
    assert (alpha / ".env").exists()
    assert (alpha / "config.local.json").exists()
    assert (beta / ".env").exists()
    assert (beta / "config.local.json").exists()
    assert "SECRET_alpha=value123" in (alpha / ".env").read_text(encoding="utf-8")


def test_batch_export_partial_failure(registry_file, capsys, passphrase_file, tmp_path):
    """一个项目 manifest 坏了，另一个正常——批量不中断，报告部分失败。"""
    # 把 alpha 的 manifest 改坏
    alpha_path = None
    beta_path = None
    # 从注册表读出路径
    import tomllib
    data = tomllib.loads(registry_file.read_text(encoding="utf-8"))
    for proj in data["projects"]:
        if proj["name"] == "alpha":
            alpha_path = Path(proj["path"])
        elif proj["name"] == "beta":
            beta_path = Path(proj["path"])

    (alpha_path / ".migrate" / "manifest.toml").write_text(
        "not valid toml {{{", encoding="utf-8",
    )

    code, payload, _ = run_cli(capsys, ["--json", "batch", "export",
                                         "--registry", str(registry_file),
                                         "--passphrase-file", str(passphrase_file)])
    assert code == 1  # 部分失败
    results = payload["data"]["results"]
    by_name = {r["name"]: r for r in results}
    assert by_name["alpha"]["exit"] != 0
    assert by_name["beta"]["exit"] == 0


def test_batch_export_no_enc_in_git(two_projects, registry_file, capsys, passphrase_file):
    """批量导出时，每个项目各自走单项目的 git_ignore_check。

    非 git 仓库里 git_ignore_check 返回 None（只 warn 不 error），
    所以两个项目都应该成功。这个测试主要验证批量不会因为某个项目
    的 gitignore 检查结果而错误地中断其他项目。
    单项目层面的「.enc 没被忽略则拒绝」由单项目测试覆盖。
    """
    alpha, beta = two_projects
    # alpha 的 .gitignore 有 .migrate/*（含 .enc），beta 故意只留 .env
    (beta / ".gitignore").write_text(".env\nconfig.local.json\n", encoding="utf-8")

    code, payload, _ = run_cli(capsys, ["--json", "batch", "export",
                                         "--registry", str(registry_file),
                                         "--passphrase-file", str(passphrase_file)])
    # 非 git 仓库 → git_ignore_check 返回 None → 只 warn，不 error
    # 两个项目都应该成功导出
    assert code == 0
    by_name = {r["name"]: r for r in payload["data"]["results"]}
    assert by_name["alpha"]["exit"] == 0
    assert by_name["beta"]["exit"] == 0


def test_batch_import_dry_run(registry_file, passphrase_file, capsys, two_projects):
    """dry-run 不写文件。"""
    alpha, beta = two_projects
    # 先 export
    run_cli(capsys, ["--json", "batch", "export",
                     "--registry", str(registry_file),
                     "--passphrase-file", str(passphrase_file)])
    capsys.readouterr()  # 清空

    # 删文件
    (alpha / ".env").unlink()
    (beta / ".env").unlink()

    # dry-run import
    code, payload, _ = run_cli(capsys, ["--json", "batch", "import",
                                         "--registry", str(registry_file),
                                         "--passphrase-file", str(passphrase_file),
                                         "--dry-run"])
    assert code == 0
    # dry-run 不写文件
    assert not (alpha / ".env").exists()
    assert not (beta / ".env").exists()


# ── 跨设备路径兼容 ────────────────────────────────────────────────────────


def test_batch_registry_with_tilde_paths(tmp_path, capsys, monkeypatch, passphrase_file):
    """注册表用 ~ 路径，在任何机器上都展开对。"""
    # 模拟 home 目录
    home = tmp_path / "home"
    home.mkdir()
    projects_root = home / "projects"
    projects_root.mkdir()

    # 建项目在 home 下
    for slug in ["p1", "p2"]:
        root = projects_root / slug
        (root / ".migrate").mkdir(parents=True)
        (root / ".env").write_text(f"K=v\n", encoding="utf-8")
        (root / ".gitignore").write_text(".env\n.migrate/*\n*.enc\n", encoding="utf-8")
        (root / ".migrate" / "manifest.toml").write_text(textwrap.dedent(f"""
            schema_version = 1
            project = "{slug}"
            artifacts_dir = ".migrate"
            [[items]]
            id = "env-file"
            path = ".env"
            class = "required"
            on_missing = "block"
            sensitive = true
        """), encoding="utf-8")

    # 注册表用 ~ 路径
    reg = tmp_path / "reg.toml"
    reg.write_text(textwrap.dedent("""
        schema_version = 1
        [[projects]]
        name = "p1"
        path = "~/projects/p1"
        [[projects]]
        name = "p2"
        path = "~/projects/p2"
    """), encoding="utf-8")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    code, payload, _ = run_cli(capsys, ["--json", "batch", "plan",
                                         "--registry", str(reg)])
    assert code == 0
    assert payload["data"]["project_count"] == 2

    code, payload, _ = run_cli(capsys, ["--json", "batch", "export",
                                         "--registry", str(reg),
                                         "--passphrase-file", str(passphrase_file)])
    assert code == 0
    assert all(r["exit"] == 0 for r in payload["data"]["results"])


def test_batch_registry_with_env_var_paths(tmp_path, capsys, monkeypatch, passphrase_file):
    """注册表用 $VAR 路径，跨设备/跨用户都能展开。"""
    base = tmp_path / "work"
    base.mkdir()
    root = base / "proj"
    (root / ".migrate").mkdir(parents=True)
    (root / ".env").write_text("K=v\n", encoding="utf-8")
    (root / ".gitignore").write_text(".env\n.migrate/*\n*.enc\n", encoding="utf-8")
    (root / ".migrate" / "manifest.toml").write_text(textwrap.dedent("""
        schema_version = 1
        project = "envproj"
        artifacts_dir = ".migrate"
        [[items]]
        id = "env-file"
        path = ".env"
        class = "required"
        on_missing = "block"
        sensitive = true
    """), encoding="utf-8")

    monkeypatch.setenv("WORKBASE", str(base))

    reg = tmp_path / "reg.toml"
    reg.write_text(textwrap.dedent("""
        schema_version = 1
        [[projects]]
        name = "envproj"
        path = "$WORKBASE/proj"
    """), encoding="utf-8")

    code, payload, _ = run_cli(capsys, ["--json", "batch", "plan",
                                         "--registry", str(reg)])
    assert code == 0
    assert payload["data"]["project_count"] == 1
