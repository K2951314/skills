"""env 文件健康检查：在打包前/落盘前拦住「搬到新机器才炸」的那类损坏。

为什么单独一个模块：.env 的损坏在**源机器上往往是静默的**——某种加载路径
偶然吞掉了问题，于是照常打包、照常对账，到了新机器才以看似无关的错误爆出来。
ZK-AI 2026-09-24 的真实事故：全文件 115 行 `\\r\\r\\n`，行内容尾部各带一个 CR，
新机上 `ZKAI_HOST='0.0.0.0\\r'`，`socket.bind` 报 `getaddrinfo failed`——
看起来是 DNS 问题，实际是换机包的锅。

铁律：只报结构问题（有多少行、什么形态），**绝不打印、绝不回传任何值**。
"""

from __future__ import annotations

CR = b"\r"
LF = b"\n"
CRLF = b"\r\n"
DOUBLED_CR = b"\r\r"
NUL = b"\x00"
UTF8_BOM = b"\xef\xbb\xbf"
UTF16_LE_BOM = b"\xff\xfe"
UTF16_BE_BOM = b"\xfe\xff"


def check_env_health(raw: bytes) -> list[str]:
    """返回问题描述列表；空列表 = 健康。只描述形态，不含任何值。"""
    problems: list[str] = []
    if not raw:
        return ["文件是空的（0 字节）——确认这不是误删或误建"]

    total_lines = raw.count(LF)
    doubled = raw.count(DOUBLED_CR)

    if doubled:
        # 判定：`\r\r` 数 == `\r\n` 数 ⇒ 每一行的内容尾部都多带一个 CR（全文件损坏）。
        # 少于则是局部污染。两种都要报，但严重程度说清楚。
        crlf = raw.count(CRLF)
        if doubled >= crlf > 0:
            problems.append(
                f"{doubled}/{total_lines} 行是 \\r\\r\\n（**全文件**行尾多一个 CR），"
                "每个键的值尾部都会带上回车。"
                "症状是新机器上把它们当地址/路径用时莫名失败（曾表现为 socket.bind 报 "
                "getaddrinfo failed，看着像 DNS 实际不是）。"
                "修复：把 \\r\\r\\n 全替换成 \\r\\n（改前先备份，改后逐行比对）。"
            )
        else:
            problems.append(
                f"有 {doubled} 处 \\r\\r（共 {total_lines} 行）——部分行尾多一个 CR。"
                "修复同上，改完再导。"
            )

    if raw.startswith(UTF16_LE_BOM) or raw.startswith(UTF16_BE_BOM):
        problems.append(
            "文件是 UTF-16 编码（带 BOM）。多数 dotenv 实现按 UTF-8 读它，"
            "结果是满屏乱码或解析出 0 个键。修复：转存为 UTF-8（无 BOM）。"
        )
    elif raw.startswith(UTF8_BOM):
        problems.append(
            "文件带 UTF-8 BOM。多数 dotenv 实现不剥 BOM，第一个键名会变成 "
            "'\\ufeffKEY'。修复：去掉 BOM 存为无 BOM 的 UTF-8。"
        )

    if NUL in raw:
        problems.append(
            f"文件含 {raw.count(NUL)} 个 NUL 字节——典型的 UTF-16/二进制写入痕迹，"
            "或文件被截断。这个文件不能原样搬运。"
        )

    return problems
