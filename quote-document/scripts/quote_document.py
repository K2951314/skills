# -*- coding: utf-8 -*-
"""报价单生成 — 型号 → 报价 → 正式单据（docx/pdf/png/xlsx），走后端导出路由层。

为什么要这层包装（三条都是实测踩过的）:
- 导出套件的权威实现是 ``backend/smart_quotation/api/routes_export.py`` 的
  ``/api/export/*``：列 schema、脱敏、编号分配、审计、公式注入前缀、
  RFC 5987 文件名全在那一层。绕开它自己拼 docx = 把不变量重抄一遍，
  迟早和 GUI 出去的单据不一致。
- 直接 import export 模块同样不行：编号持久化、客户编码临界区、审计落库
  都是路由层的职责。所以这里用 FastAPI TestClient **在进程内调真实路由**——
  与 GUI 走同一条代码路径，但不要求后端服务正在运行。
- 本机 ``.env`` 若同时有 ``SQ_DEV=1`` 与 ``DATABASE_URL``/``SQ_LICENSE``，
  项目会按设计拒绝启动（安全断言）。本脚本先剥掉生产信号再建 app，
  等价于「本地开发形态」——并且**只在数据库副本上操作**。

数据安全（硬约束）:
- 默认把 ``quotation.db`` 复制到临时目录再操作，退出即删。
  ``--in-place`` 才允许直连真实库（会写编号序列与 audit_events），
  用于「必须使用生产编号序列」的场景，需显式加。
- ``DATABASE_URL`` 指向 PostgreSQL 时直接拒绝：脚本只支持 SQLite 快照。
  生产 PG 请走后端 HTTP 接口（``--base-url`` 未实现，属 P2）。

用法:
    py -3.13 "$SKILL/scripts/quote_document.py" "D4*11*50*d4" --qty 3 \
        --buyer 青岛示例机械有限公司 --format docx --out .\\out
    py -3.13 "$SKILL/scripts/quote_document.py" "D4*11*50*d4" "WNMG080408" \
        --customer probe-cust-1 --number QT-20261119-001 --format pdf
    py -3.13 "$SKILL/scripts/quote_document.py" --check     # 环境与能力自检

退出码: 0=生成成功; 1=询价无命中; 2=项目/依赖/库定位失败; 3=导出失败; 4=参数非法。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


# --------------------------------------------------------------------------- #
# 项目定位（换机可移植：环境变量严格语义 + 常见根探测）
# --------------------------------------------------------------------------- #
def resolve_project_root() -> Path:
    """定位智能询价项目根（判据：backend/smart_quotation/engine.py）。

    显式设置了 SQ_HOME 就必须有效——设错直接报错，绝不静默改用别处的库
    （报价工具用错库是事故）。
    """
    def valid(p: Path) -> bool:
        return (p / "backend" / "smart_quotation" / "engine.py").is_file()

    env = os.environ.get("SQ_HOME", "").strip()
    if env:
        cand = Path(env)
        if valid(cand):
            return cand
        raise SystemExit(
            "[SQ_HOME 无效] 环境变量 SQ_HOME=%s 下找不到 "
            "backend/smart_quotation/engine.py。\n"
            "请修正 SQ_HOME，或取消设置让脚本自动探测。" % env
        )
    repo_root = Path(__file__).resolve().parents[2]
    candidates = [repo_root.parent / "智能询价", Path.home() / "智能询价"]
    for cand in candidates:
        if valid(cand):
            return cand
    tried = "\n  ".join(str(c) for c in candidates)
    raise SystemExit(
        "[项目定位失败] 找不到智能询价项目（判据 "
        "backend/smart_quotation/engine.py）。\n"
        "请设置环境变量 SQ_HOME 指向项目根目录后重试。已探测:\n  " + tried
    )


# --------------------------------------------------------------------------- #
# 环境准备：剥掉生产信号，让 create_app 以「本地开发」形态启动
# --------------------------------------------------------------------------- #
#: 与 ``api/factory.py`` 的生产信号清单一致（DB_PATH 仅非默认值才算信号，
#: 这里一并清掉，改由 cwd 保证 quotation.db 存在，避免触发 download_db 网络请求）。
_PRODUCTION_SIGNALS = (
    "DATABASE_URL",
    "ALLOW_ORIGINS",
    "SQ_LICENSE",
    "DB_PATH",
)


def load_project_env(project_root: Path) -> None:
    """加载项目 ``.env``（与 ``backend/smart_quotation/__main__.py`` 同口径）。

    导出能力（``SQ_LIBREOFFICE_PATH``）、三菱凭证等都只写在 ``.env`` 里；
    不加载会让 PDF/PNG 被误判成「缺依赖」。只补**尚未设置**的键，
    不覆盖调用方显式传的环境变量。
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        print("提示：未安装 python-dotenv，跳过 .env 加载（PDF/PNG 可能被误判为不可用）。",
              file=sys.stderr)
        return
    env_file = project_root / ".env"
    if env_file.is_file():
        load_dotenv(dotenv_path=str(env_file), override=False)


def build_client(project_root: Path, db_path: Path):
    """建进程内 TestClient（与 GUI 同一条路由层）。返回 (client, store)。"""
    sys.path.insert(0, str(project_root))
    load_project_env(project_root)
    os.environ["SQ_DEV"] = "1"
    for key in _PRODUCTION_SIGNALS:
        os.environ.pop(key, None)
    # create_app 在 cwd 下检查 quotation.db 是否存在，不存在会尝试从
    # Supabase 下载。切到项目根让该检查直接通过（本地开发无备份配置时
    # download_db 也只是打一行日志，但别让它无谓地碰网络）。
    if Path.cwd() != project_root:
        os.chdir(project_root)
    try:
        from fastapi.testclient import TestClient
        from backend.smart_quotation.api.factory import create_app
        from backend.smart_quotation.store import QuotationStore
    except Exception as exc:
        raise SystemExit(
            "[引擎加载失败] %s: %s\n请确认用 Python 3.13 且项目依赖已安装"
            "（fastapi / httpx / python-docx / openpyxl 等）。"
            % (type(exc).__name__, exc)
        )
    store = QuotationStore(db_path=str(db_path))
    return TestClient(create_app(store)), store


def run_self_check(project_root: Path) -> int:
    """自检：定位、依赖、导出能力、库形态、PG 模式。只读，不建 app。"""
    sys.path.insert(0, str(project_root))
    load_project_env(project_root)
    report = {"project_root": str(project_root)}
    ok = True
    try:
        import fastapi
        import httpx  # noqa: F401
        report["fastapi"] = fastapi.__version__
        import docx  # noqa: F401
        import openpyxl
        report["openpyxl"] = openpyxl.__version__
    except Exception as exc:
        report["dependencies_error"] = "%s: %s" % (type(exc).__name__, exc)
        ok = False

    db = project_root / "quotation.db"
    report["quotation_db"] = str(db)
    report["quotation_db_exists"] = db.is_file()
    report["db_mode"] = (
        "postgres" if os.environ.get("DATABASE_URL", "").strip().lower()
        .startswith(("postgres://", "postgresql://")) else "sqlite"
    )
    if not report.get("dependencies_error"):
        try:
            from backend.smart_quotation.export.office import LibreOfficeConverter
            caps = LibreOfficeConverter.from_env().capabilities()
            report["export"] = {
                "pdf": caps["pdf"]["available"],
                "png": caps["png"]["available"],
                "pdf_reason": caps["pdf"]["reason"],
            }
        except Exception as exc:
            report["export_error"] = "%s: %s" % (type(exc).__name__, exc)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if ok else 2


# --------------------------------------------------------------------------- #
# 询价
# --------------------------------------------------------------------------- #
def quote_items(store, queries, company_id):
    """逐型号询价，返回 (items, 未命中型号)。

    公司不存在 / 该公司没有已发布配置时，引擎抛 ``LookupError`` —— 那不是
    「这个型号查不到」，而是「你在问一个不存在的公司」。两者混在一起会让
    用户去改型号，所以原样上抛，由 ``main`` 转成明确的中文错误。
    """
    from backend.smart_quotation.engine import QuotationEngine

    engine = QuotationEngine(store)
    items, missing = [], []
    for query in queries:
        rows = engine.quote(query, company_id=company_id)
        if not rows:
            missing.append(query)
            continue
        for row in rows:
            fields = dict(row.get("fields") or {})
            item = {
                "item_key": row.get("item_key"),
                "fields": fields,
                # 顶层 quote_price 是后端唯一校验权威；行字段里的旧值会被覆盖，
                # 这里两者保持一致，避免「界面价」与「计费价」分叉。
                "quote_price": fields.get("quote_price"),
            }
            if row.get("warnings"):
                item["warnings"] = row["warnings"]
            items.append(item)
    return items, missing


# --------------------------------------------------------------------------- #
# 导出
# --------------------------------------------------------------------------- #
def confirm_number(store, company_id, kind, date_str, customer_id, customer_code):
    """经路由层同一实现确认编号（持久序列，含客户编码临界区与审计）。"""
    from backend.smart_quotation.api.routes_export import _confirm_document_number

    payload = {"kind": kind, "date": date_str}
    if customer_id:
        payload["customer_id"] = customer_id
    elif customer_code:
        payload["customer_code"] = customer_code
    else:
        raise SystemExit(
            "[参数缺失] 需要 --customer（已建档客户 ID）或 --customer-code（客户编码）"
            "才能确认自动编号；也可以直接用 --number 指定手工单号。"
        )
    try:
        return _confirm_document_number(store, company_id, payload)["number"]
    except SystemExit:
        raise
    except Exception as exc:
        # ExportError 带稳定 code；其它异常保留类型名，便于和项目日志对齐。
        code = getattr(exc, "code", None)
        message = getattr(exc, "message", None) or str(exc)
        print("[编号确认失败] %s%s: %s"
              % (type(exc).__name__, ("/" + str(code)) if code else "", message),
              file=sys.stderr)
        if code == "export.customer_required":
            print("  提示：--customer 必须是当前公司的**直接**成员公司；"
                  "手工单号可用 --number。", file=sys.stderr)
        raise SystemExit(3)


def post_export(client, payload):
    """POST /api/export/{kind}；返回 (状态码, 字节|None, 头, 错误说明|None)。"""
    kind = payload.get("kind") or "quote"
    response = client.post("/api/export/%s" % kind, json=payload)
    headers = {k.lower(): v for k, v in response.headers.items()}
    if response.status_code == 200:
        return 200, response.content, headers, None
    detail = response.text
    try:
        body = response.json()
        detail = body.get("detail") if isinstance(body, dict) else body
        if isinstance(detail, dict):
            detail = detail.get("message") or json.dumps(detail, ensure_ascii=False)
    except ValueError:
        pass
    return response.status_code, None, headers, str(detail)


def filename_from_headers(headers, fallback):
    """按 RFC 5987 filename* 解析下载名（与前端 export.js 同一口径）。"""
    from urllib.parse import unquote

    raw = headers.get("content-disposition", "")
    match = re.search(r"filename\*=UTF-8''([^;]+)", raw)
    if match:
        return unquote(match.group(1))
    match = re.search(r'filename="([^"]+)"', raw)
    return match.group(1) if match else fallback


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(
        description="报价单生成：型号 → 报价 → 正式单据（走后端导出路由层）",
    )
    ap.add_argument("queries", nargs="*", help="型号/规格，可多个（空格分隔）")
    ap.add_argument("--qty", type=float, help="统一数量（必须 ≥1；默认 1）")
    ap.add_argument("--company", default="default", help="公司 ID（默认 default）")
    ap.add_argument("--customer", help="已建档客户公司 ID（补买方字段并派生客户编码）")
    ap.add_argument("--customer-code", help="手工客户编码（未建档场景）")
    ap.add_argument("--buyer", help="买方名称（未传 --customer 时必填）")
    ap.add_argument("--buyer-tax-id", default="", help="买方税号")
    ap.add_argument("--buyer-contact", default="", help="买方联系人")
    ap.add_argument("--buyer-phone", default="", help="买方电话")
    ap.add_argument("--buyer-address", default="", help="买方地址")
    ap.add_argument("--number", help="手工单号（不传则按客户自动编号）")
    ap.add_argument("--date", help="单据日期 YYYY-MM-DD（默认今天）")
    ap.add_argument("--valid-until", help="有效期 YYYY-MM-DD（可选；不传则不显示）")
    ap.add_argument("--payment-method", choices=["prepaid", "cod", "deposit"],
                    help="付款方式（可选；只存稳定 code）")
    ap.add_argument("--kind", choices=["quote", "contract"], default="quote")
    ap.add_argument("--format", choices=["docx", "pdf", "png", "xlsx"], default="docx")
    ap.add_argument("--locale", choices=["zh", "en", "ja", "ru", "uz"], default="zh")
    ap.add_argument("--columns", help="导出列字段 key，逗号分隔（默认配置里的复制列）")
    ap.add_argument("--seal", action="store_true", help="盖主章（需公司已上传印章）")
    ap.add_argument("--seam-seal", action="store_true",
                    help="盖骑缝章（仅 PDF/PNG，且依赖主章）")
    ap.add_argument("--png-mode", choices=["pages", "long"], default="pages")
    ap.add_argument("--db", help="SQLite 库路径（默认复制项目 quotation.db 到临时目录）")
    ap.add_argument("--in-place", action="store_true",
                    help="直接使用 --db/项目库（会写编号序列与审计）；默认用临时副本")
    ap.add_argument("--out", default=".", help="输出目录（默认当前目录）")
    ap.add_argument("--json", action="store_true", help="输出机器可读结果")
    ap.add_argument("--check", action="store_true", help="只做环境与能力自检")
    args = ap.parse_args()

    project_root = resolve_project_root()
    # --out 在 chdir 之前解析成绝对路径，否则切到项目根后相对路径会漂移。
    out_dir = Path(args.out).expanduser()
    if not out_dir.is_absolute():
        out_dir = Path.cwd() / out_dir

    if args.check:
        return run_self_check(project_root)

    if not args.queries:
        ap.print_usage(sys.stderr)
        print("需要至少一个型号。用 --check 只做环境自检。", file=sys.stderr)
        return 4

    if args.qty is not None and args.qty < 1:
        print("[参数非法] --qty 必须 ≥1（后端会拒绝 0/负数/NaN，别指望静默归一）。",
              file=sys.stderr)
        return 4

    if args.seam_seal and not args.seal:
        # 骑缝章依赖主章。后端会报 export.seam_requires_seal，但**不能靠它兜底**：
        # 提前收口才不会出现「用户以为盖了骑缝章，拿到的文件却没有」这种静默降级。
        print("[参数非法] --seam-seal 必须同时给 --seal（骑缝章依赖主章）。\n"
              "只想要主章：去掉 --seam-seal；两个都要：--seal --seam-seal。",
              file=sys.stderr)
        return 4

    if args.seal and args.format == "xlsx":
        print("[参数非法] XLSX 不支持盖章（后端 export.seal_format）。"
              "请改用 --format docx/pdf/png。", file=sys.stderr)
        return 4

    if args.seam_seal and args.format in ("xlsx", "docx"):
        print("[参数非法] 骑缝章只对 PDF/PNG 有效（后端 export.seam_format）。",
              file=sys.stderr)
        return 4

    # 单号来源必须在建 app 之前确定：既不是「询价失败」也不是「导出失败」，
    # 而是参数没给全。提前收口，退出码才不会被误读成「没有这个型号」。
    if not args.number and not (args.customer or args.customer_code):
        print("[参数非法] 需要三选一：--number（手工单号） / --customer（已建档客户）"
              " / --customer-code（客户编码）。\n"
              "  后两者会走公司级持久序列，自动编号形如 QT-<客户编码>-YYYYMMDD-001。",
              file=sys.stderr)
        return 4

    if not args.customer and not args.buyer:
        print("[参数非法] 未传 --customer 时买方名称 --buyer 必填"
              "（后端 export.buyer_required）。", file=sys.stderr)
        return 4

    db_url = os.environ.get("DATABASE_URL", "").strip().lower()
    if db_url.startswith(("postgres://", "postgresql://")):
        print(
            "[不支持 PG 直连] 本脚本只操作 SQLite 快照。生产 PostgreSQL 请走后端 "
            "HTTP 接口（/api/export/*），或取消 DATABASE_URL 后再跑。",
            file=sys.stderr,
        )
        return 2

    temp_dir = None
    source_db = Path(args.db).expanduser() if args.db else project_root / "quotation.db"
    if args.in_place:
        db_path = source_db
        if not db_path.is_file():
            print("[数据库不存在] %s" % db_path, file=sys.stderr)
            return 2
    else:
        if not source_db.is_file():
            print("[数据库不存在] %s" % source_db, file=sys.stderr)
            return 2
        # 临时库副本放在**本技能目录**下的 .tmp/，不跟随进程 TEMP。
        # 实测本会话的 TEMP 被指到了别的项目（ZK-AI/.tmp），副本会散落到
        # 不相干的项目里；而「一个技能自己产生的中间文件」应该跟着它自己走。
        staging = Path(__file__).resolve().parent / ".tmp"
        staging.mkdir(parents=True, exist_ok=True)
        temp_dir = tempfile.mkdtemp(prefix="sq-doc-", dir=str(staging))
        db_path = Path(temp_dir) / "quotation.db"
        shutil.copy2(source_db, db_path)

    try:
        client, store = build_client(project_root, db_path)

        try:
            items, missing = quote_items(store, args.queries, args.company)
        except LookupError as exc:
            print("[公司不可用] %s\n"
                  "  检查 --company 的取值：可用公司查 quotation.db 的 companies 表，"
                  "且该公司需要已发布配置。" % exc, file=sys.stderr)
            return 2
        for query in missing:
            print("[询价无命中] %r（公司 %s）" % (query, args.company), file=sys.stderr)
        if not items:
            return 1
        warnings = [w for item in items for w in item.get("warnings") or []]
        for warning in dict.fromkeys(warnings):
            print("[数据告警] %s（该行报价按 0 计）" % warning, file=sys.stderr)
        if args.qty is not None:
            for item in items:
                item["qty"] = args.qty

        from datetime import date

        date_str = args.date or date.today().isoformat()
        number = args.number or confirm_number(
            store, args.company, args.kind, date_str,
            args.customer, args.customer_code,
        )

        buyer = {
            "name": args.buyer or "",
            "tax_id": args.buyer_tax_id,
            "contact_name": args.buyer_contact,
            "contact_phone": args.buyer_phone,
            "address": args.buyer_address,
        }
        document = {"number": number, "date": date_str, "buyer": buyer}
        if args.customer:
            document["customer_id"] = args.customer
        if args.valid_until:
            # 有效期是可选字段：不填就不显示，绝不补默认天数。
            document["valid_until"] = args.valid_until
        if args.payment_method:
            document["payment_method"] = args.payment_method

        options = {}
        if args.columns:
            options["columns"] = [c.strip() for c in args.columns.split(",") if c.strip()]
        if args.format == "png":
            options["png_mode"] = args.png_mode

        payload = {
            "kind": args.kind,
            "format": args.format,
            "document": document,
            "items": items,
            "options": options,
            # 语种在 payload 顶层：后端 parse_document_draft 读 payload["locale"]。
            # 之前 CLI 收了 --locale 却从未放进请求，等于参数无效（静默无效比报错更糟）。
            "locale": args.locale,
        }
        if args.format != "xlsx":
            payload["include_seal"] = bool(args.seal)
            if args.format in ("pdf", "png"):
                # 骑缝章依赖主章：后端会报 export.seam_requires_seal，
                # 这里提前收口，别让用户拿到一个难懂的 422。
                options["seam_seal"] = bool(args.seam_seal and args.seal)

        status, content, headers, error = post_export(client, payload)
        if status != 200 or content is None:
            print("[导出失败] HTTP %s: %s" % (status, error), file=sys.stderr)
            if "libreoffice_missing" in str(error):
                print("  提示：PDF/PNG 依赖 LibreOffice。设置 SQ_LIBREOFFICE_PATH "
                      "指向 soffice.exe，或改用 --format docx/xlsx。",
                      file=sys.stderr)
            if "number_not_confirmed" in str(error):
                print("  提示：传了 document 就必须带已确认的单号；"
                      "用 --number 手工指定，或去掉自定义买方走自动编号。",
                      file=sys.stderr)
            return 3

        out_dir.mkdir(parents=True, exist_ok=True)
        fallback = "%s.%s" % (number, args.format)
        target = out_dir / filename_from_headers(headers, fallback)
        # 绝不覆盖已有单据：财务文件被静默替换是事故，不是便利。
        # 同名时追加 -2/-3，并在输出里说明改成了哪个名字（不静默改名）。
        if target.exists():
            base, suffix = target.stem, target.suffix
            index = 2
            while True:
                candidate = out_dir / ("%s-%d%s" % (base, index, suffix))
                if not candidate.exists():
                    print("[同名文件已存在] %s → 改存为 %s"
                          % (target.name, candidate.name), file=sys.stderr)
                    target = candidate
                    break
                index += 1
        target.write_bytes(content)

        result = {
            "ok": True,
            "document_number": number,
            "kind": args.kind,
            "format": args.format,
            "file": str(target),
            "bytes": len(content),
            "rows": len(items),
            "items": [
                {
                    "item_key": item["item_key"],
                    "quote_price": str(item["quote_price"]),
                    "qty": item.get("qty", 1),
                }
                for item in items
            ],
            "missing": missing,
            "warnings": list(dict.fromkeys(warnings)),
            "in_place": bool(args.in_place),
        }
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("单据已生成: %s" % target)
            print("单号: %s | 格式: %s | 行数: %d | 字节: %d"
                  % (number, args.format, len(items), len(content)))
            for item in result["items"]:
                print("  - %s × %s @ %s"
                      % (item["item_key"], item["qty"], item["quote_price"]))
            if missing:
                print("未命中型号: %s" % ", ".join(missing))
            if args.seal:
                print("已请求盖章（主章%s）"
                      % ("+骑缝" if options.get("seam_seal") else ""))
            if not args.in_place:
                print("提示：本次在数据库副本上跑，未消耗真实编号序列。"
                      "要用生产序号加 --in-place。")
        return 0
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
        # 顺手收掉空的 .tmp 暂存目录，别在技能目录里留垃圾
        staging = Path(__file__).resolve().parent / ".tmp"
        try:
            staging.rmdir()
        except OSError:
            pass  # 非空或不存在都不影响


if __name__ == "__main__":
    sys.exit(main())
