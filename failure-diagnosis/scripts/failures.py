# -*- coding: utf-8 -*-
"""失效对策查询 — 直读 smart-selection 真源，复用网页端权威查询语义。

实现不自带规则：定位项目后 import web/src/brands_api.py 的 failures_lookup
（14 类失效同义词归一 + op 过滤必带「综合」），与 /api/failures 完全同源，
避免第三份实现漂移。brands_api 只依赖标准库，无需起 Flask。

用法:
    py -3.13 failure-diagnosis/scripts/failures.py 崩刃 --op 铣削
    py -3.13 failure-diagnosis/scripts/failures.py 积屑瘤
    py -3.13 failure-diagnosis/scripts/failures.py 振动 --brand 三菱 --json
    py -3.13 failure-diagnosis/scripts/failures.py --list     # 列出失效模式与工序

退出码: 0=有命中; 1=无命中（附可用模式提示）; 2/3=定位/加载失败。
"""
import argparse
import os
import sys
from pathlib import Path

# Windows 控制台可能是 GBK：对策句含日文/符号，强制 UTF-8 输出防 UnicodeEncodeError
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def resolve_project_root() -> Path:
    """定位 smart-selection 项目根（判据：web/src/brands_api.py 存在）。

    显式设置了 SS_HOME 就必须有效（设错报错，绝不静默回退）；没设置才探测。
    """
    def valid(p: Path) -> bool:
        return (p / "web" / "src" / "brands_api.py").is_file()

    env = os.environ.get("SS_HOME", "").strip()
    if env:
        cand = Path(env)
        if valid(cand):
            return cand
        raise SystemExit(
            "[SS_HOME 无效] 环境变量 SS_HOME=%s 下找不到 web/src/brands_api.py。\n"
            "请修正 SS_HOME，或取消设置让脚本自动探测。" % env
        )
    repo_root = Path(__file__).resolve().parents[2]
    candidates = [repo_root.parent / "smart-selection", Path.home() / "smart-selection"]
    for cand in candidates:
        if valid(cand):
            return cand
    tried = "\n  ".join(str(c) for c in candidates)
    raise SystemExit(
        "[项目定位失败] 找不到 smart-selection（判据 web/src/brands_api.py）。\n"
        "请设置环境变量 SS_HOME 指向项目根目录后重试。已探测:\n  " + tried
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="失效对策查询（样本原文，14 类失效 × 多品牌）")
    ap.add_argument("query", nargs="?", help="失效关键词，如 崩刃 / 积屑瘤 / 振动 / 寿命短")
    ap.add_argument("--op", help="工序过滤：车削/铣削/钻削/攻丝/镗削/切槽/切断/螺纹（自动带上「综合」通用对策）")
    ap.add_argument("--brand", help="只看某品牌（如 三菱 / 京瓷 / 泰珂洛）")
    ap.add_argument("--json", action="store_true", help="输出与 /api/failures 同构的 JSON")
    ap.add_argument("--list", action="store_true", help="列出全部失效模式与工序")
    args = ap.parse_args()

    root = resolve_project_root()
    sys.path.insert(0, str(root / "web"))
    try:
        from src.brands_api import failures_lookup, failure_modes, ops
    except Exception as e:
        print("[加载失败] %s: %s" % (type(e).__name__, e), file=sys.stderr)
        return 3

    if args.list:
        print("失效模式: " + " / ".join(failure_modes()))
        print("工序: " + " / ".join(ops()))
        return 0

    if not args.query:
        ap.print_usage(sys.stderr)
        print("需要查询词（或 --list 看可用模式）。", file=sys.stderr)
        return 1

    hits = failures_lookup(args.query, op=args.op)
    if args.brand:
        for h in hits:
            h["brands"] = [b for b in h["brands"] if args.brand in b["brand"]]
        hits = [h for h in hits if h["brands"]]

    if args.json:
        import json
        print(json.dumps(hits, ensure_ascii=False, indent=2))
        return 0 if hits else 1

    if not hits:
        print("无命中：%r（op=%s, brand=%s）" % (args.query, args.op or "全部", args.brand or "全部"))
        print("可用失效模式: " + " / ".join(failure_modes()))
        print("提示：关键词走同义词归一（如 打刀/断刀→破损/断裂，震刀→振动/颤振）；换模式词再试。")
        return 1

    for h in hits:
        print("【%s】%d 句对策 / %d 品牌" % (h["mode"], h["total"], len(h["brands"])))
        for b in h["brands"]:
            opstr = " ".join("%s×%d" % (o, n) for o, n in sorted(b["ops"].items()))
            print("  ── %s （%s） 来源: %s" % (b["brand"], opstr, ", ".join(b["refs"])))
            for s in b["sentences"]:
                print("     · " + s)
        print()
    print("（对策为样本原文句子，每品牌最多显示 8 句；不替厂家发挥。完整结构用 --json。）")
    return 0


if __name__ == "__main__":
    sys.exit(main())