"""report 层测试：JSON 契约、表格渲染、脱敏。"""

from __future__ import annotations

import json

from migrate_engine.report import Report, human_size, redact, sha8


def test_json_contract_shape():
    rep = Report(json_mode=True, title="t")
    rep.row(id="a", cls="required", size="1.0 KB", sha256_8="abcd1234", result="ok")
    rep.warn("w")
    payload = json.loads(rep.render())
    assert set(payload) == {"ok", "exit", "data"}
    assert payload["data"]["items"][0]["id"] == "a"
    assert payload["data"]["totals"]["warnings"] == 1
    assert payload["data"]["warnings"] == ["w"]


def test_human_render_table_and_warnings():
    rep = Report(title="标题")
    rep.row(id="env", cls="required", size="1.0 KB", sha256_8="abcd1234", result="已写入")
    rep.warn("缺一个建议项")
    text = rep.render()
    assert "标题" in text and "env" in text and "已写入" in text
    assert "[警告] 缺一个建议项" in text


def test_redact_removes_secret_values(secret_values):
    rep = Report()
    rep.say(f"JWT_SECRET={secret_values[0]}")
    rep.warn(f"ADMIN_API_KEY={secret_values[1]}")
    text = redact(rep.render(), secret_values)
    assert secret_values[0] not in text
    assert secret_values[1] not in text
    assert "***" in text


def test_helpers():
    assert human_size(512) == "512 B"
    assert human_size(2048) == "2.0 KB"
    assert sha8("0123456789abcdef") == "01234567"
    assert sha8(None) == "-"
