"""CLI 测试：doctor / manifest / plan 的退出码与输出契约。"""

from __future__ import annotations

import json

from migrate_engine.cli import main


def run_cli(capsys, argv: list[str]) -> tuple[int, dict | None, str]:
    code = main(argv)
    out = capsys.readouterr().out
    payload = None
    if "--json" in argv:
        payload = json.loads(out)
    return code, payload, out


def test_doctor_ok(capsys):
    code, payload, _ = run_cli(capsys, ["doctor", "--json"])
    assert code == 0
    assert payload["data"]["capabilities"]["python_ok"] is True


def test_manifest_init_then_validate(project_with_manifest, capsys):
    # init 覆盖已存在 → 冲突退出码 3
    code, _, _ = run_cli(capsys, ["--root", str(project_with_manifest), "manifest", "init",
                                  "--project", "other"])
    assert code == 3
    code, payload, _ = run_cli(capsys, ["--root", str(project_with_manifest), "manifest",
                                        "validate", "--json"])
    assert code == 0
    assert payload["data"]["project"] == "fake-proj"
    assert payload["data"]["item_count"] == 4


def test_manifest_init_print_only(fake_project, capsys):
    code, _, out = run_cli(capsys, ["--root", str(fake_project), "manifest", "init",
                                    "--project", "fresh-proj", "--print"])
    assert code == 0
    assert 'project = "fresh-proj"' in out
    assert not (fake_project / ".migrate" / "manifest.toml").exists()


def test_plan_lists_items_and_suggestions(git_project, capsys):
    (git_project / ".migrate" / "manifest.toml").write_text(
        """
schema_version = 1
project = "gp"
[[items]]
id = "env-file"
path = ".env"
class = "required"
on_missing = "block"
""",
        encoding="utf-8",
    )
    code, payload, _ = run_cli(capsys, ["--root", str(git_project), "plan", "--json"])
    assert code == 0
    ids = [row["id"] for row in payload["data"]["items"]]
    assert ids == ["env-file"]
    suggestions = payload["data"]["suggestions"]
    assert any("config.local.json" in s for s in suggestions)
    assert any("data/app.db" in s for s in suggestions)
    # node_modules 是 refuse 类，不该出现在候选区
    assert not any("node_modules" in s for s in suggestions)


def test_plan_missing_required_blocks(project_with_manifest, capsys):
    (project_with_manifest / ".env").unlink()
    code, payload, _ = run_cli(capsys, ["--root", str(project_with_manifest), "plan", "--json"])
    assert code == 3
    rows = {row["id"]: row["result"] for row in payload["data"]["items"]}
    assert "阻断" in rows["env-file"]
    assert any("缺失" in e for e in payload["data"]["errors"])


def test_plan_without_manifest_suggests_init(fake_project, capsys):
    code, _, out = run_cli(capsys, ["--root", str(fake_project), "plan"])
    assert code == 2
    assert "manifest init" in out


def test_plan_profile_filter(project_with_manifest, capsys):
    code, payload, _ = run_cli(capsys, ["--root", str(project_with_manifest),
                                        "plan", "--profile", "machine-only", "--json"])
    assert code == 0
    ids = [row["id"] for row in payload["data"]["items"]]
    assert "app-db" not in ids
    assert "env-file" in ids


def test_plan_output_is_redacted(project_with_manifest, capsys, secret_values):
    code, _, out = run_cli(capsys, ["--root", str(project_with_manifest), "plan"])
    assert code == 0
    for secret in secret_values:
        assert secret not in out
