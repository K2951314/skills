# -*- coding: utf-8 -*-
"""抽取结果校验：两条独立线，各判各的。

A 数值连续出现（硬）
    CSV 的每个二元组/三元组，必须在原文的**数字流**里连续出现
    （三元组在 PDF 里就是 x 相邻的三个数）。抓「凭空造出来的值」「跨行串值」。

B 回原文子串（硬）
    CSV 的每个分类值，去掉空白后必须是该页全文的子串。抓「凭空多出的值」。

⚠ A 和 B 必须各自独立判，不能写成「B 过了就 continue」：
  真实事故 vc='64 - 12'——原文里确实有 64-12（那是每英寸牙数 TPI），B 层会放过，
  只有 A 层（要求 min<max 的三元组连续出现）+ 值域才能抓出来。

⚠ 数字流不能按 round(y,1) 排：三元组**中间值加粗**，y 比两侧高 0.2pt，
  四舍五入后会被排到前面，三元组当场不连续（p356 全表 45 组全报「找不到」）。
  必须先按 y 聚成视觉行，再行内按 x 排。

用法：
    python verify_run.py 样本.pdf 主表.csv --page-col page \
        --triplet "vc=vc_min,vc_rec,vc_max" \
        --triplet "ap=ap_min,ap_rec,ap_max" \
        --pair    "f=f_min,f_max" \
        --substr  "grade,chip,material"
"""
import argparse
import csv
import io
import re
import sys
from collections import Counter

import fitz

NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def load_spans(pg):
    out = []
    for b in pg.get_text("rawdict")["blocks"]:
        if b.get("type") != 0:
            continue
        for ln in b["lines"]:
            for sp in ln["spans"]:
                t = "".join(c["c"] for c in sp.get("chars", []))
                if t.strip():
                    x0, y0 = sp["bbox"][0], sp["bbox"][1]
                    out.append((y0, x0, t))
    out.sort()
    return out


def number_stream(pg, dy=3.2):
    """整页数字流：先按 y 聚成视觉行（和簇首比），行内按 x 排，再取数字。"""
    lines = []
    for y, x, t in load_spans(pg):
        if lines and (y - lines[-1]["y"]) <= dy:
            lines[-1]["items"].append((x, t))
        else:
            lines.append({"y": y, "items": [(x, t)]})
    seq = []
    for ln in lines:
        for x, t in sorted(ln["items"]):
            for s in NUM_RE.findall(t.replace(",", "").replace("，", "")):
                seq.append(float(s))
    return seq


def has_run(seq, vals, tol=1e-9):
    """vals 是否作为**连续**子序列出现在 seq 里。"""
    n = len(vals)
    if n == 0:
        return True
    for i in range(len(seq) - n + 1):
        if all(abs(seq[i + j] - vals[j]) <= tol for j in range(n)):
            return True
    return False


def parse_specs(specs):
    """'vc=vc_min,vc_rec,vc_max' → ('vc', ['vc_min','vc_rec','vc_max'])"""
    out = []
    for s in specs or []:
        name, _, cols = s.partition("=")
        cols = [c.strip() for c in cols.split(",") if c.strip()]
        if not name.strip() or len(cols) < 2:
            raise SystemExit("格式应为 '名称=列1,列2[,列3]'，收到: %r" % s)
        out.append((name.strip(), cols))
    return out


# 同一标签在样本里因字体/换行有变体，抽取后要归一化（奧→奥、・→·、全角→半角）。
# ⚠ 归一化必须**两边都做**：只归一化 CSV 侧、拿未归一化的原文当 haystack，
#   会把「归一化」本身误判成「值不存在」（踩过：p353 的 不锈钢/(奥氏体系) 繁体「奧」）。
GLYPH_FIX = {"奧": "奥", "・": "·", "，": ",", "（": "(", "）": ")"}


def squash(s):
    s = (s or "")
    for a, b in GLYPH_FIX.items():
        s = s.replace(a, b)
    return re.sub(r"[\s/()（）]", "", s)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("csv")
    ap.add_argument("--page-col", default="page")
    ap.add_argument("--triplet", action="append", help="'vc=vc_min,vc_rec,vc_max'，可多次")
    ap.add_argument("--pair", action="append", help="'f=f_min,f_max'，可多次")
    ap.add_argument("--substr", default="", help="要求回原文子串的列，逗号分隔")
    ap.add_argument("--dy", type=float, default=3.2)
    ap.add_argument("--show", type=int, default=6, help="每类最多打印几条失败")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    with io.open(args.csv, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print("空表")
        return 1
    if args.page_col not in rows[0]:
        print("!! CSV 没有列 %r；没有页码就无法校验" % args.page_col)
        return 1

    groups = parse_specs(args.triplet) + parse_specs(args.pair)
    substr_cols = [c.strip() for c in args.substr.split(",") if c.strip()]
    pages = sorted({int(r[args.page_col]) for r in rows if (r.get(args.page_col) or "").strip()})
    print("CSV: %s | 行数 %d | 页数 %d" % (args.csv, len(rows), len(pages)))

    doc = fitz.open(args.pdf)
    streams, texts = {}, {}
    for p in pages:
        if not (1 <= p <= doc.page_count):
            print("  !! p%d 超出 PDF 页数 %d" % (p, doc.page_count))
            continue
        pg = doc[p - 1]
        streams[p] = number_stream(pg, args.dy)
        texts[p] = squash(pg.get_text())
    doc.close()

    ok = True

    # ── A 数值连续出现 ──
    if groups:
        print("\n── A 二元组/三元组在原文数字流里连续出现 ──")
        for p in pages:
            seq = streams.get(p)
            if seq is None:
                continue
            checked, miss, badorder = 0, [], []
            for r in rows:
                if (r.get(args.page_col) or "").strip() != str(p):
                    continue
                for name, cols in groups:
                    vals = []
                    for c in cols:
                        v = (r.get(c) or "").strip()
                        if v:
                            try:
                                vals.append(float(v))
                            except ValueError:
                                vals = []
                                break
                    if len(vals) < 2:
                        continue
                    checked += 1
                    if len(vals) >= 3 and vals[0] > vals[1]:
                        badorder.append((name, vals))
                        continue
                    if not has_run(seq, vals):
                        miss.append((name, vals, r.get("material") or r.get("grade") or ""))
            flag = "OK " if not miss and not badorder else "BAD"
            print("  %s p%d 校验 %d 组 | 找不到 %d | 非单调 %d" % (
                flag, p, checked, len(miss), len(badorder)))
            for name, vals, who in miss[:args.show]:
                print("      !! %s=%s 不在原文数字流 (%s)" % (name, vals, who))
            for name, vals in badorder[:args.show]:
                print("      !! %s=%s 非单调（min>mid，可能串列）" % (name, vals))
            if miss or badorder:
                ok = False

    # ── B 回原文子串 ──
    if substr_cols:
        print("\n── B 分类值回原文子串（去空白/斜杠/括号）──")
        for c in substr_cols:
            if c not in rows[0]:
                print("  !! 列不存在:", c)
                ok = False
                continue
            for p in pages:
                hay = texts.get(p, "")
                want = {squash(r.get(c)) for r in rows
                        if (r.get(args.page_col) or "").strip() == str(p)}
                want.discard("")
                lost = sorted(v for v in want if v not in hay)
                print("  %s p%-5d %-10s %2d 个值 | 原文找不到 %d %s" % (
                    "OK " if not lost else "BAD", p, c, len(want), len(lost),
                    lost[:args.show] if lost else ""))
                if lost:
                    ok = False

    print("\n" + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
