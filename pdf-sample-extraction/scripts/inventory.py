# -*- coding: utf-8 -*-
"""定分母：按**版式特征**扫全本，生成页清单。

为什么不按「页面上有 Vc 字样」捞页：那会把正文段落页、目录页、材质 lineup 页全捞进来，
抽出来的东西自然不是表（真实事故：1394 行里四列齐全只有 3 行）。

按**列名组合**分类才是对的：真参数表一定同时出现「材料域 + 数值列」这组列名。

用法：
    # 一种版式
    python inventory.py 样本.pdf --layout "drilling=加工材料,加工径,Vc" --out 页清单.csv

    # 多种版式（每种一个 --layout）
    python inventory.py 样本.pdf \
        --layout "full_matrix=加工形态,推荐断屑槽,Vc" \
        --layout "milling=加工材料,fz,Vc" \
        --layout "drilling=加工材料,加工径,Vc" \
        --out 页清单.csv

    # 加 --tables 同时记录 find_tables 检出的表格数（慢，全本跑几分钟）
    python inventory.py 样本.pdf --layout "..." --tables
"""
import argparse
import csv
import re
import sys
from collections import Counter, defaultdict

import fitz

# 数值区间 token：'30~120' / '250 - 350' / '0.05-0.5'
# 真参数表一定成片出现；目录 / 说明 / 材质 lineup 页没有。
RANGE_RE = re.compile(r"\d+(?:\.\d+)?\s*[~～\-–]\s*\d+(?:\.\d+)?")
NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def parse_layouts(specs):
    out = []
    for s in specs or []:
        name, _, kws = s.partition("=")
        kws = [k.strip() for k in kws.split(",") if k.strip()]
        if not name.strip() or not kws:
            raise SystemExit("--layout 格式应为 '名称=关键词1,关键词2'，收到: %r" % s)
        out.append((name.strip(), kws))
    return out


def chapter_map(doc, level=1):
    """从书签建「页 → 章名」。书签页码是 PDF 物理页序（1-based），别用印刷页码。"""
    m = {}
    cur = ""
    for lvl, title, page in doc.get_toc():
        if lvl <= level:
            cur = title.strip()
            m[page] = cur
    return m


def chap_of(m, p):
    best = ""
    for st in sorted(m):
        if p >= st:
            best = m[st]
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--layout", action="append", help="'名称=关键词1,关键词2'，可多次")
    ap.add_argument("--title", action="append", default=[],
                    help="「这类表」的标题词（如 推荐切削参数）；无列名命中但标题在 → 记 other")
    ap.add_argument("--min-range", type=int, default=6,
                    help="标题在但数值区间少于这个数 → 判噪声（目录/说明页），默认 6")
    ap.add_argument("--pages", default="", help="只扫这些页：'1-200' / '1,3,5-7'")
    ap.add_argument("--tables", action="store_true", help="同时记录 find_tables 检出数（慢）")
    ap.add_argument("--out", default="页清单.csv")
    ap.add_argument("--no-chapter", action="store_true", help="不读书签建章名")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    layouts = parse_layouts(args.layout)
    if not layouts and not args.title:
        raise SystemExit("至少给一个 --layout 或 --title")

    doc = fitz.open(args.pdf)
    cm = {} if args.no_chapter else chapter_map(doc)
    if args.pages:
        rng = []
        for part in args.pages.replace(" ", "").split(","):
            if "-" in part:
                a, b = part.split("-", 1)
                rng.extend(range(int(a), int(b) + 1))
            elif part:
                rng.append(int(part))
        pages = [p for p in rng if 1 <= p <= doc.page_count]
    else:
        pages = list(range(1, doc.page_count + 1))

    rows = []
    n_empty = 0
    for p in pages:
        pg = doc[p - 1]
        t = pg.get_text()
        if not t.strip():
            n_empty += 1
            continue

        kinds = [name for name, kws in layouts if all(k in t for k in kws)]
        n_range = len(RANGE_RE.findall(t))
        if not kinds:
            if not any(w in t for w in args.title):
                continue
            # 标题在，但数值不成片 → 目录 / 用法说明 / 材质 lineup
            kinds = ["other" if n_range >= args.min_range else "noise"]

        row = {
            "page": p,
            "chapter": chap_of(cm, p) if cm else "",
            "layout": "+".join(kinds),
            "n_range": n_range,
            "n_num": len(NUM_RE.findall(t)),
            "n_kw": sum(t.count(k) for _, kws in layouts for k in kws),
        }
        if args.tables:
            try:
                row["n_tables"] = len(pg.find_tables().tables)
            except Exception:
                row["n_tables"] = -1
        rows.append(row)
    doc.close()

    if not rows:
        print("没有命中任何页。检查 --layout 的关键词是否和样本用词一致（简繁/中日文差异）。")
        return

    with open(args.out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    c = Counter(r["layout"] for r in rows)
    print("扫描 %d 页（无文字层 %d）→ 候选 %d 页 → %s" % (len(pages), n_empty, len(rows), args.out))
    for k, v in c.most_common():
        by_ch = Counter(r["chapter"] for r in rows if r["layout"] == k).most_common(6)
        print("  %-22s %4d 页 | 按章 %s" % (k, v, by_ch))
    real = [r for r in rows if r["layout"] != "noise"]
    print("\n可解析页合计（去 noise）: %d ；噪声页（只有标题无数值）: %d" % (
        len(real), c.get("noise", 0)))
    if args.tables:
        no_tbl = [r["page"] for r in real if r.get("n_tables", 0) == 0]
        print("其中 find_tables 认不出表格的页 %d 个（这些要用坐标模型）: %s" % (
            len(no_tbl), no_tbl[:20]))


if __name__ == "__main__":
    main()
