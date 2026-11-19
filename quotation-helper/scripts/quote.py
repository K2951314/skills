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

项目定位（换机可移植，勿写死盘符）:
    环境变量 SQ_HOME 优先 → 技能仓库的同级目录 → 用户 home 下。
    找不到就中文报错并提示设置 SQ_HOME，绝不静默回退。
"""
import argparse
import json
import os
import sys
from pathlib import Path


def resolve_backend_root() -> str:
    """定位智能询价项目根（判据：backend/smart_quotation/engine.py 存在）。

    语义：显式设置了 SQ_HOME 就必须有效（设错直接报错，绝不悄悄改用别处的
    项目——报价工具用错库是事故）；没设置才按常见根探测。
    """
    def valid(p: Path) -> bool:
        return (p / "backend" / "smart_quotation" / "engine.py").is_file()

    env = os.environ.get("SQ_HOME", "").strip()
    if env:
        cand = Path(env)
        if valid(cand):
            return str(cand)
        raise SystemExit(
            "[SQ_HOME 无效] 环境变量 SQ_HOME=%s 下找不到 backend/smart_quotation/engine.py。\n"
            "请修正 SQ_HOME，或取消设置让脚本自动探测。" % env
        )
    # 技能仓库（my-skills）与项目通常在同一父目录下
    repo_root = Path(__file__).resolve().parents[2]
    candidates = [repo_root.parent / "智能询价", Path.home() / "智能询价"]
    for cand in candidates:
        if valid(cand):
            return str(cand)
    tried = "\n  ".join(str(c) for c in candidates)
    raise SystemExit(
        "[项目定位失败] 找不到智能询价项目（判据 backend/smart_quotation/engine.py）。\n"
        "请设置环境变量 SQ_HOME 指向项目根目录后重试。已探测:\n  " + tried
    )


def main() -> int:
    backend_root = resolve_backend_root()

    ap = argparse.ArgumentParser(description="智能询价 — 本地报价查询")
    ap.add_argument("query", help="型号/规格/助记码，如 D4*11*50*d4")
    ap.add_argument("--company", default="default", help="公司 ID（默认 default）")
    ap.add_argument(
        "--db",
        default=os.path.join(backend_root, "quotation.db"),
        help="报价数据库路径（默认 <项目根>/quotation.db）",
    )
    args = ap.parse_args()

    sys.path.insert(0, backend_root)
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