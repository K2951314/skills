#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""报价查询 CLI — 封装 smart-quotation 报价引擎。

直接调 QuotationEngine，绕过 HTTP 层的认证/限流，适合本地 skill 快速出报价。
用法:
    py quote.py <型号> [--company default] [--db 路径]
示例:
    py quote.py "D4*11*50*d4"
    py quote.py "D4*11*50*d4" --company default
输出: JSON（item_key / 报价 / 折扣 / copy_text）
"""
import argparse
import json
import os
import sys

# 智能询价 backend 根目录
BACKEND_ROOT = r"E:/Ingulf/智能询价"


def main() -> int:
    ap = argparse.ArgumentParser(description="智能询价 — 本地报价查询")
    ap.add_argument("query", help="型号/规格/助记码，如 D4*11*50*d4")
    ap.add_argument("--company", default="default", help="公司 ID（默认 default）")
    ap.add_argument(
        "--db",
        default=os.path.join(BACKEND_ROOT, "quotation.db"),
        help="报价数据库路径（默认 <backend>/quotation.db）",
    )
    args = ap.parse_args()

    sys.path.insert(0, BACKEND_ROOT)
    try:
        from backend.smart_quotation.store import QuotationStore
        from backend.smart_quotation.engine import QuotationEngine
    except Exception as e:
        print(f"[引擎加载失败] {type(e).__name__}: {e}", file=sys.stderr)
        print("请确认 Python >= 3.13 且已安装依赖（fastapi/sqlalchemy 等）。", file=sys.stderr)
        return 2

    try:
        store = QuotationStore(db_path=args.db)
    except Exception as e:
        print(f"[数据库打开失败] {args.db} → {e}", file=sys.stderr)
        return 3

    engine = QuotationEngine(store)
    try:
        results = engine.quote(args.query, company_id=args.company)
    except LookupError as e:
        print(f"[查询失败] {e}", file=sys.stderr)
        return 4
    except Exception as e:
        print(f"[引擎错误] {type(e).__name__}: {e}", file=sys.stderr)
        return 5

    print(json.dumps({"query": args.query, "company_id": args.company,
                      "count": len(results), "results": results},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
