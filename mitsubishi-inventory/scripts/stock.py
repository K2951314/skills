#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三菱库存查询 CLI — 封装 mitsubishi_stock 查询引擎。

用法:
    py stock.py <型号> [材质] [--model X --material Y]
示例:
    py stock.py WNMG080408-MA UC5115
    py stock.py --model WNMG080408 --material UC5115
输出: JSON（上海库存 / 日本库存 / 状态 / 原始消息）+ 诊断
"""
import argparse
import json
import sys

BACKEND_ROOT = r"E:/Ingulf/智能询价"


def main() -> int:
    ap = argparse.ArgumentParser(description="三菱库存查询")
    ap.add_argument("args_query", nargs="*", help="型号 [材质]，如 WNMG080408-MA UC5115")
    ap.add_argument("--model", help="型号（含后缀，如 WNMG080408-MA）")
    ap.add_argument("--material", help="材质/牌号，如 UC5115")
    args = ap.parse_args()

    model = args.model
    material = args.material
    if not model and args.args_query:
        model = args.args_query[0]
    if not material and len(args.args_query) > 1:
        material = args.args_query[1]

    if not model:
        print("用法: py stock.py <型号> [材质]", file=sys.stderr)
        print("  示例: py stock.py WNMG080408-MA UC5115", file=sys.stderr)
        return 1

    sys.path.insert(0, BACKEND_ROOT)
    try:
        from backend.smart_quotation.mitsubishi_stock import get_engine
    except Exception as e:
        print(f"[引擎加载失败] {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    eng = get_engine()

    # 先做联网自检（金丝雀）。三菱 GWT-RPC 无 schema，引擎靠启发式解析，
    # 官网改版后可能不报错但返回错误数字 —— 自检失败即 fail-closed，绝不返回可疑库存。
    ready = eng.ensure_ready()
    canary = eng.ensure_canary()
    diag = {"ready": bool(ready), "canary": bool(canary)}

    if not ready or not canary:
        print(json.dumps({
            "ok": False,
            "error": "三菱官网自检失败，库存查询已 fail-closed 保护",
            "diagnostics": diag,
            "hint": "通常原因：(1) 官网改版/结构变更 (2) 三菱账号凭证失效(401) "
                    "(3) 网络不通。请检查 MMC_USERNAME/MMC_PASSWORD/MMC_COOKIE "
                    "或 config.ini 的 [account]，或三菱官网是否改版。",
        }, ensure_ascii=False, indent=2))
        return 3

    # search(model_val, material_val) → (shanghai, japan, exists, ?, msg)
    try:
        shanghai, japan, exists, extra, msg = eng.search(model, material)
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"},
                         ensure_ascii=False, indent=2))
        return 4

    result = {
        "ok": True,
        "model": model,
        "material": material,
        "shanghai_stock": shanghai,
        "japan_stock": japan,
        "exists": bool(exists),
        "message": msg,
        "diagnostics": diag,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
