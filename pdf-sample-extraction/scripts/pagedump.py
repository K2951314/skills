# -*- coding: utf-8 -*-
"""单页勘察：打印 span 坐标 / 视觉行 / 检出的表格。

抽取任何一页之前先跑这个，别猜版式。三种模式对应三种看法：

  spans   原始 span（含 bbox），看「一个值被拆成了几个 span」
  lines   按 y 聚成视觉行、行内按 x 排，看「一行到底有哪些值、谁跟谁对齐」
  tables  find_tables() 检出的表格（行列数 + 前两行），先判有没有框线

用法：
    python pagedump.py 样本.pdf 515
    python pagedump.py 样本.pdf 515 --mode lines --x0 40 --x1 552 --y0 90 --y1 790
    python pagedump.py 样本.pdf 510-520 --mode tables
    python pagedump.py 样本.pdf 515 --mode spans --max 200
"""
import argparse
import re
import sys

import fitz

# 行聚类默认容差。3.2 是实测夹出来的：
#   行内加粗值与常规值差 0.22pt（要并）
#   折行格 '0.02 - 0.07 -' + '0.1' 差 2.6pt（要并）
#   相邻子行 / 形态行与参数行差 3.97pt（要分）
DEFAULT_DY = 3.2


def load_spans(pg):
    """[(y0, x0, text, size)]，只取文本块。用 rawdict 才能拿到私用字体字符。"""
    out = []
    for b in pg.get_text("rawdict")["blocks"]:
        if b.get("type") != 0:
            continue
        for ln in b["lines"]:
            for sp in ln["spans"]:
                t = "".join(c["c"] for c in sp.get("chars", []))
                if t.strip():
                    x0, y0, x1, y1 = sp["bbox"]
                    out.append((y0, x0, t, sp.get("size", 0)))
    out.sort()
    return out


def visual_lines(spans, dy=DEFAULT_DY, y0=0.0, y1=1e9, x0=0.0, x1=1e9):
    """按 y 聚成视觉行。

    ⚠ 必须和**簇首**比（lines[-1]['y']），不能和上一个 span 比：
      按相邻间距会链式串行（135.3→138.8→142.0→143.5 每步都 <dy，两行并成一行）。
    """
    lines = []
    for y, x, t, sz in spans:
        if not (y0 <= y <= y1) or not (x0 <= x <= x1):
            continue
        if lines and (y - lines[-1]["y"]) <= dy:
            lines[-1]["items"].append((x, t))
        else:
            lines.append({"y": y, "items": [(x, t)]})
    for ln in lines:
        ln["items"].sort()
    return lines


def parse_pages(s):
    """'515' / '510-520' / '1,3,5-7' → [int]"""
    out = []
    for part in s.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


def cmd_spans(pg, pno, args):
    spans = load_spans(pg)
    # 先按区域过滤、再截断：反了的话 --x0/--y0 会把前 max 条全滤掉，看起来像「没有 span」
    sel = [(y, x, t, sz) for y, x, t, sz in spans
           if args.x0 <= x <= args.x1 and args.y0 <= y <= args.y1]
    print("== p%d  spans=%d（区域内 %d）==" % (pno, len(spans), len(sel)))
    print("%8s %8s %6s  %s" % ("y", "x", "size", "text"))
    for y, x, t, sz in sel[:args.max]:
        print("%8.2f %8.2f %6.2f  %r" % (y, x, sz, t))


def cmd_lines(pg, pno, args):
    lines = visual_lines(load_spans(pg), args.dy, args.y0, args.y1, args.x0, args.x1)
    print("== p%d  visual_lines=%d  (dy=%.1f) ==" % (pno, len(lines), args.dy))
    for i, ln in enumerate(lines[:args.max]):
        cells = " | ".join("%.1f:%s" % (x, t) for x, t in ln["items"])
        print("%3d y=%7.2f  %s" % (i, ln["y"], cells))


def cmd_tables(pg, pno, args):
    try:
        tf = pg.find_tables()
    except AttributeError:
        print("== p%d  find_tables 不可用（PyMuPDF 版本过低）==" % pno)
        return
    print("== p%d  tables=%d ==" % (pno, len(tf.tables)))
    for i, t in enumerate(tf.tables):
        print("  [%d] bbox=(%.1f,%.1f,%.1f,%.1f) rows=%d cols=%d" % (
            i, t.bbox[0], t.bbox[1], t.bbox[2], t.bbox[3], t.row_count, t.col_count))
        rows = t.extract()
        for r in rows[:3]:
            print("      %s" % " | ".join(re.sub(r"\s+", " ", (c or "")).strip() for c in r))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("pages", help="515 / 510-520 / 1,3,5-7")
    ap.add_argument("--mode", default="all", choices=["spans", "lines", "tables", "all"])
    ap.add_argument("--x0", type=float, default=0.0)
    ap.add_argument("--x1", type=float, default=1e9)
    ap.add_argument("--y0", type=float, default=0.0)
    ap.add_argument("--y1", type=float, default=1e9)
    ap.add_argument("--dy", type=float, default=DEFAULT_DY)
    ap.add_argument("--max", type=int, default=80)
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    doc = fitz.open(args.pdf)
    for pno in parse_pages(args.pages):
        if not (1 <= pno <= doc.page_count):
            print("!! p%d 超出范围（共 %d 页）" % (pno, doc.page_count))
            continue
        pg = doc[pno - 1]
        if not pg.get_text().strip():
            print("== p%d  无文字层（扫描件/纯图）==" % pno)
            continue
        if args.mode in ("spans", "all"):
            cmd_spans(pg, pno, args)
        if args.mode in ("lines", "all"):
            cmd_lines(pg, pno, args)
        if args.mode in ("tables", "all"):
            cmd_tables(pg, pno, args)
        print()
    doc.close()


if __name__ == "__main__":
    main()
