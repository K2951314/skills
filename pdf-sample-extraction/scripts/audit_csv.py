# -*- coding: utf-8 -*-
"""既有产出体检：接入前必跑。

分母定义：**CSV 全表行数**（不是「脚本跑完的行数」）。分六段判：

  A 列结构   每列非空率 / 唯一值 / 整列全空（R6）
  B 值域     能否解析成数值、是否在物理区间内、含中文段落、残缺区间（R1/R2）
  C 成组性   真正的参数表必须「材料域 + 数值列」同时成片出现，只看 Vc 有字不算
  D 主键     重复主键（R5）。⚠ 先判「合法多行（并集）」还是「重复抽取」
  E 型号     截断（`...` / `**`）、含空格逗号（多个牌号挤进型号列）（R3/R4）
  F 判定     四列齐全率低于阈值 → 判「不是表」，回阶段 2 重定页

用法：
    python audit_csv.py 全量_切削参数.csv \
        --cols "加工材料,硬度,加工形态,推荐材质,Vc,ap,f" \
        --limits "Vc=1:2000,ap=0.01:25,f=0.001:5" \
        --group "加工材料,Vc,ap,f" --min-complete 0.5

    python audit_csv.py 车削刀片.csv --key-col 型号 --key-col page
"""
import argparse
import csv
import io
import re
import sys
from collections import Counter

UNIT_RE = re.compile(r"(m/min|mm/min|mm/rev|mm/t|mm|°|％|%)")
SEP_RE = re.compile(r"[～~\-–—]")
DIGIT_RE = re.compile(r"^\d+(?:\.\d+)?$")
TRUNC_RE = re.compile(r"\.\.\.|\*\*")
BROKEN_RANGE_RE = re.compile(r"^\d+\.?\d*\s*[-–—~～]$")
DASH_RE = re.compile(r"^[-–—~～]$")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def parse_limits(s):
    out = {}
    for part in (s or "").split(","):
        part = part.strip()
        if not part:
            continue
        k, _, v = part.partition("=")
        lo, _, hi = v.partition(":")
        out[k.strip()] = (float(lo), float(hi))
    return out


def nums(v):
    """抽出字符串里的全部数字；非纯数值形（带中文/字母）返回 None。

    允许 '80' / '80～120' / '80-120' / '0.15 - 0.25'，单位会被剥掉。
    """
    s = (v or "").strip()
    if not s:
        return None
    core = UNIT_RE.sub("", s).strip()
    core = SEP_RE.sub(" ", core).strip()
    toks = core.split()
    if not toks:
        return None
    for t in toks:
        if not DIGIT_RE.match(t):
            return None
    return [float(t) for t in toks]


def load(path):
    with io.open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--cols", default="", help="要报的列，逗号分隔；默认全部列")
    ap.add_argument("--limits", default="", help="'Vc=1:2000,ap=0.01:25'")
    ap.add_argument("--group", default="", help="要求同时非空且可解析的列，逗号分隔")
    ap.add_argument("--min-complete", type=float, default=0.0,
                    help="成组齐全率低于此值判「不是表」，默认不判")
    ap.add_argument("--page-col", default="page")
    ap.add_argument("--key-col", action="append", default=[], help="主键列，可多次")
    ap.add_argument("--strict", action="store_true", help="发现残缺/越界/截断就非零退出")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    rows = load(args.csv)
    n = len(rows)
    if n == 0:
        print("空表")
        return 1
    cols = [c.strip() for c in args.cols.split(",") if c.strip()] or list(rows[0].keys())
    limits = parse_limits(args.limits)
    problems = 0

    print("源文件:", args.csv)
    print("总行数（分母）:", n)
    print()

    print("── A 列结构 ─────────────────────────────────────────────")
    print("%-14s %8s %8s %8s" % ("列", "非空", "非空率", "唯一值"))
    empty_cols = []
    for c in cols:
        vals = [(r.get(c) or "").strip() for r in rows]
        ne = [v for v in vals if v]
        rate = 100.0 * len(ne) / n
        print("%-14s %8d %7.1f%% %8d" % (c, len(ne), rate, len(set(ne))))
        if not ne:
            empty_cols.append(c)
    if empty_cols:
        problems += len(empty_cols)
        print("  R6 整列全空:", empty_cols)
    print()

    if limits:
        print("── B 值域（分母=该列非空行）─────────────────────────────")
        for c, (lo, hi) in limits.items():
            if c not in rows[0]:
                print("  !! 列不存在:", c)
                continue
            ne = [(r.get(c) or "").strip() for r in rows]
            ne = [v for v in ne if v]
            parsed = [nums(v) for v in ne]
            ok = [p for p in parsed if p]
            print("%-6s 非空 %5d | 可解析 %5d (%.1f%% of 非空)" % (
                c, len(ne), len(ok), 100.0 * len(ok) / len(ne) if ne else 0.0))
            oob = [(v, p) for v, p in zip(ne, parsed)
                   if p and any(x < lo or x > hi for x in p)]
            prose = [v for v in ne if CJK_RE.search(v)]
            broken = [v for v in ne if BROKEN_RANGE_RE.match(v)]
            dash = [v for v in ne if DASH_RE.match(v)]
            print("     越界 %d %s" % (len(oob), ("例: " + str(oob[:3])) if oob else ""))
            print("     含中文 %d (%.1f%%) %s" % (
                len(prose), 100.0 * len(prose) / len(ne) if ne else 0.0,
                ("例: " + repr(prose[0])[:50]) if prose else ""))
            print("     残缺区间 %d %s | 裸 '-' %d" % (
                len(broken), ("例: " + str(broken[:3])) if broken else "", len(dash)))
            problems += len(oob) + len(broken)
        print()

    group = [c.strip() for c in args.group.split(",") if c.strip()]
    if group:
        print("── C 成组性（真参数表 = 材料域 + 数值列同时有）───────")
        # 标签列（如「加工材料」）只要求非空；只有 --limits 里声明过的列才要求可解析为数值。
        num_cols = {c for c in group if c in limits}
        both = [r for r in rows if all((r.get(c) or "").strip() for c in group)]
        parsed_ok = [r for r in both
                     if all(nums(r.get(c) or "") for c in num_cols)]
        print("指定列全部非空: %d (%.1f%%)" % (len(both), 100.0 * len(both) / n))
        print("指定列全部可解析为数值: %d (%.1f%%)" % (len(parsed_ok), 100.0 * len(parsed_ok) / n))
        rate = len(parsed_ok) / n
        if args.min_complete and rate < args.min_complete:
            print("\n结论：**这份产出不是表**（成组齐全 %.1f%% < %.0f%%），不能接入。"
                  % (rate * 100, args.min_complete * 100))
            print("      回样本重新定页（先跑 inventory.py），别在错的对象上修字段。")
            problems += 1
        print()

    if args.key_col:
        print("── D 主键重复（先判是「合法多行/并集」还是「重复抽取」）──")
        keys = [(tuple((r.get(k) or "").strip() for k in args.key_col)) for r in rows]
        c = Counter(keys)
        dup = [(k, v) for k, v in c.items() if v > 1]
        print("主键 %s | 唯一 %d | 重复 %d 组 | 重复行数 %d" % (
            args.key_col, len(c), len(dup), sum(v - 1 for _, v in dup)))
        for k, v in dup[:5]:
            print("   x%-3d %s" % (v, k))
        print()

    print("── E 型号列（截断 / 含空格逗号）──────────────────────")
    cand = [c for c in cols if re.search(r"型号|part|model|code", c, re.I)]
    for c in cand:
        vals = [(r.get(c) or "").strip() for r in rows]
        trunc = [v for v in vals if TRUNC_RE.search(v)]
        spaced = [v for v in vals if re.search(r"[,，]\s*\S", v)]
        print("%-10s 截断 %d %s | 含逗号 %d %s" % (
            c, len(trunc), ("例: " + str(trunc[:3])) if trunc else "",
            len(spaced), ("例: " + str(spaced[:3])) if spaced else ""))
        problems += len(trunc)
    if not cand:
        print("  （没找到型号列，用 --cols 指定）")
    print()

    if args.page_col in rows[0]:
        print("── 页码分布 ───────────────────────────────────────────")
        pc = Counter((r.get(args.page_col) or "").strip() for r in rows)
        print(", ".join("p%s:%d" % (k or "空", v) for k, v in pc.most_common(15)))
        print("涉及页码数:", len([k for k in pc if k]))
        nul = pc.get("", 0)
        if nul:
            print("  !! page 为空 %d 行（违反「每行带页码」）" % nul)
            problems += nul
        print()

    print("── F 判定 ─────────────────────────────────────────────")
    print("问题计数:", problems)
    if problems and args.strict:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
