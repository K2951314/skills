"""汇报层：人看表格 + --json 契约 + 脱敏。

脱敏铁律：汇报只允许出现 键名 / 体积 / 哈希前 8 位 / 存在性 / 命令结果。
禁止出现口令、环境变量的值、私钥 PEM、token、连接串密码。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

REDACTED = "***"


def redact(text: str, secrets: list[str]) -> str:
    """把 secrets 中每个非空值替换成 ***。测试用它断言输出不泄密。"""
    for secret in secrets:
        if secret and len(secret) >= 4 and secret in text:
            text = text.replace(secret, REDACTED)
    return text


def human_size(n: int | float | None) -> str:
    if n is None:
        return "-"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def sha8(digest: str | None) -> str:
    return digest[:8] if digest else "-"


@dataclass
class ResultRow:
    id: str
    cls: str = "-"
    size: str = "-"
    sha256_8: str = "-"
    result: str = "-"
    note: str = ""


@dataclass
class Report:
    """收集一次操作的结构化结果；emit() 时按模式输出。

    json_mode 下输出单对象 {"ok", "exit", "data"}；
    人看模式下输出中文短表。两种模式内容一致，只有排版不同。
    """

    json_mode: bool = False
    ok: bool = True
    exit_code: int = 0
    title: str = ""
    rows: list[ResultRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    data: dict = field(default_factory=dict)

    # ── 收集 ──
    def say(self, msg: str) -> None:
        self.lines.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def error(self, msg: str) -> None:
        self.errors.append(msg)
        self.ok = False

    def row(self, **kwargs) -> None:
        self.rows.append(ResultRow(**kwargs))

    def set_data(self, key: str, value) -> None:
        self.data[key] = value

    def finish(self, exit_code: int) -> int:
        self.exit_code = exit_code
        self.ok = exit_code == 0 and not self.errors
        return exit_code

    # ── 输出 ──
    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "exit": self.exit_code,
            "data": {
                **self.data,
                "items": [vars(r) for r in self.rows],
                "totals": {
                    "items": len(self.rows),
                    "warnings": len(self.warnings),
                    "errors": len(self.errors),
                },
                "warnings": list(self.warnings),
                "errors": list(self.errors),
            },
        }

    def render(self) -> str:
        if self.json_mode:
            return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
        out: list[str] = []
        if self.title:
            out.append(self.title)
        for line in self.lines:
            out.append(line)
        if self.rows:
            headers = ("id", "类别", "体积", "sha256", "结果", "备注")
            body = [headers]
            for r in self.rows:
                body.append((r.id, r.cls, r.size, r.sha256_8, r.result, r.note))
            widths = [max(len(str(row[i])) for row in body) for i in range(len(headers))]
            for row in body:
                out.append("  " + "  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
        for w in self.warnings:
            out.append(f"  [警告] {w}")
        for e in self.errors:
            out.append(f"  [错误] {e}")
        return "\n".join(out)

    def emit(self) -> int:
        print(self.render())
        return self.exit_code
