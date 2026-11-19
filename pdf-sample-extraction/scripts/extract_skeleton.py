# -*- coding: utf-8 -*-
"""抽取骨架：配置驱动的 find_tables() 抽取器 + 块级连坐隔离。

为什么需要它：每次新样本都从零写抽取器，会把已经踩过的坑再踩一遍。
这个骨架把「有框线表格」的通用流程固定下来，新样本只改命令行参数：

  1. find_tables() 取表（表头关键词定位，不写死列号）
  2. 表头行数自动数（1~4 行；第一行常是「下限－推荐－上限」跨列占位，写死 1 行会产出 0 行）
  3. 幻影行合并（只有身份列、没有参数值的行并回上一行）
  4. 子行展开：n 由 --expand 列的三元组个数定（不由牌号个数定）
  5. 其余列按 '/' 拆，且拆出的每段必须过该列的合法性检查，否则整格共用
  6. 标签列向下继承（--carry）+ 标签归一化（--norm-cols / --nospace-cols / --tight-cols）
  7. 值域 + 单调性校验，**一个表块有一条不过，整块进低置信表**

用法（京瓷 11 列全矩阵，已验证 184/184 与人工验收产出一致）：
    python extract_skeleton.py 样本.pdf --pages 353,354,514,515 \
        --col-map "ISO=iso,加工材料=material,硬度=hardness,切削领域=domain,加工形态=form,推荐断屑槽=chip,推荐材质=grade,刀尖=re,速度=vc,切深=ap,进给=f" \
        --expand vc --carry material,iso,hardness \
        --split form,chip,grade,re,vc,ap,f --triplet vc,ap,f \
        --norm-cols material,form,chip --nospace-cols domain --tight-cols iso,hardness \
        --limits "vc=1:2000,ap=0.01:25,f=0.001:5" \
        --require material --out 主表.csv --bad _低置信.csv

--col-map 的写法是「表头关键词=字段名」。表头匹配前会把 '/' 和空白全去掉，
所以 `推荐/断屑槽` 也能匹配 `推荐断屑槽`；多行表头会**拼起来**再匹配，
所以关键词写「速度」而不是「下限－推荐－上限」那种跨列占位。

⚠ 验收要做**值级多重集比对**，不要只比行数：本骨架曾出现「184 行全对、但身份列全空」
（fields 取错字典一侧），只看行数会直接放行。见 references/07-骨架验收记录.md。

无框线表格不要用这个骨架，按 references/02 的列模型 7 条自己写。
"""
import argparse
import csv
import io
import re
import sys

import fitz

TRIPLET_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*$")
GRADE_RE = re.compile(r"^[A-Z0-9][A-Z0-9\-]{1,9}$")
RE_RE = re.compile(r"^[\d.]+$")
FORM_VALS = {"连续", "断续", "轻断续", "强断续"}
NUM_RE = re.compile(r"^\d+(?:\.\d+)?$")


def head_key(s):
    """表头归一化：去 '/' 和空白。"""
    return re.sub(r"[\s/]+", "", s or "")


def cell_text(v):
    """单元格归一化：换行当 '/'（可能是换行也可能是分隔符，交给拆列逻辑判）。"""
    return re.sub(r"\s+", " ", (v or "").replace("\n", "/")).strip()


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


def parse_triplet(s):
    """'250 - 300 - 350' → (250.0, 300.0, 350.0)

    ⚠ 必须先去掉千分位逗号：p515 有 `200 - 400 -1,000`，
      带逗号匹配会让整格解析失败，进而触发块级连坐把整页隔离（踩过）。
    """
    t = (s or "").replace(",", "").replace("，", "").strip()
    m = TRIPLET_RE.match(t)
    if not m:
        return None
    try:
        return tuple(float(x) for x in m.groups())
    except ValueError:
        return None


GLYPH_FIX = {"奧": "奥", "・": "·", "，": ",", "（": "(", "）": ")"}


def norm_label(s):
    """标签归一化。不做的话下游按材料筛必漏（材料 13 种收敛到 11 种就是这么来的）。"""
    s = (s or "").strip()
    for a, b in GLYPH_FIX.items():
        s = s.replace(a, b)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\(\s+", "(", s)
    s = re.sub(r"\s+\)", ")", s)
    s = re.sub(r",\s*", ", ", s)
    s = re.sub(r"/等$", "等", s)
    return s.strip()


def norm_tight(s):
    """连'/'和空白一起去掉：硬度 'HB/≤/300' → 'HB≤300'、ISO 'P*' → 'P'。"""
    s = (s or "").replace("*", "").replace("＊", "")
    return re.sub(r"[\s/]+", "", s)


def norm_nospace(s):
    """只去空白、保留 '/'：切削领域 '精加工~ 半精加工' → '精加工~半精加工'。"""
    return re.sub(r"\s+", "", s or "")


def valid_part(field, p, triplet_fields):
    """拆出来的每一段必须过该列的合法性检查，否则整格共用。"""
    p = p.strip()
    if not p:
        return False
    if field in triplet_fields:
        return parse_triplet(p) is not None
    if field == "grade":
        return bool(GRADE_RE.match(p))
    if field == "re":
        return bool(RE_RE.match(p))
    if field == "form":
        return p in FORM_VALS
    if field == "chip":
        return len(p) <= 8
    return True


def split_cell(s, field, n, triplet_fields):
    """把单元格拆成 n 段；拆不了/不合法 → 整格共用（返回 n 个相同值）。"""
    s = (s or "").strip()
    if n <= 1 or not s:
        return [s] * n
    parts = [p.strip() for p in s.split("/") if p.strip()]
    if len(parts) != n:
        return [s] * n
    if not all(valid_part(field, p, triplet_fields) for p in parts):
        return [s] * n
    return parts


def map_cols(header_rows, col_map):
    """表头多行拼起来匹配 → {field: col_idx}。不写死列号。"""
    names = []
    width = max(len(r) for r in header_rows)
    for j in range(width):
        blob = head_key("".join(cell_text(r[j]) if j < len(r) else "" for r in header_rows))
        names.append(blob)
    cols = {}
    for field, kw in col_map.items():
        k = head_key(kw)
        for j, t in enumerate(names):
            if k and k in t:
                cols[field] = j
                break
    return cols, names


def merge_phantom(rows, cols, param_fields):
    """只有身份列、没有参数值的行 → 并回上一行（用 '/' 连接）。"""
    out, merged = [], 0
    for r in rows:
        get = lambda f: cell_text(r[cols[f]]) if cols.get(f) is not None and cols[f] < len(r) else ""
        has_num = any(get(f) for f in param_fields)
        if not has_num and out:
            for f, j in cols.items():
                v = get(f)
                if v and j < len(out[-1]):
                    prev = cell_text(out[-1][j])
                    out[-1][j] = (prev + "/" + v) if prev else v
            merged += 1
            continue
        out.append(list(r))
    return out, merged


def detect_header_rows(rows, col_map, require, expand, maxhdr):
    """自动数表头行数。

    表头可能是 1~4 行，而且第一行常是「下限－推荐－上限」这种**跨列占位**，
    真列名在第二行——按固定 1 行会把列名行当成数据行（踩过：产出 0 行）。
    做法：从 1 到 maxhdr 逐个试，取第一个「必需字段都能找到、且有数据行能解出 expand 值」的行数。
    """
    for h in range(1, min(maxhdr, len(rows)) + 1):
        cols, _ = map_cols(rows[:h], col_map)
        if any(f not in cols for f in require):
            continue
        ej = cols.get(expand)
        if ej is None:
            continue
        for r in rows[h:]:
            if ej < len(r) and parse_triplet(cell_text(r[ej])):
                return h, cols
    return 1, map_cols(rows[:1], col_map)[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--pages", required=True, help="1,3,5-7")
    ap.add_argument("--col-map", required=True,
                    help="'表头关键词=字段名,...'，如 '加工材料=material,速度=vc'")
    ap.add_argument("--expand", required=True, help="子行数由哪一列的三元组个数决定（如 vc）")
    ap.add_argument("--header-rows", default="auto",
                    help="表头行数；默认 auto 自动数（跨列占位表头必须用 auto）")
    ap.add_argument("--max-header-rows", type=int, default=4)
    ap.add_argument("--triplet", default="", help="逗号分隔：这些字段按 min/rec/max 拆三列")
    ap.add_argument("--split", default="", help="逗号分隔：允许按 '/' 拆的字段")
    ap.add_argument("--carry", default="", help="逗号分隔：空则向下继承的标签列")
    ap.add_argument("--norm-cols", default="",
                    help="逗号分隔：做标签归一化的列（繁简/中点/括号空格/行末斜杠）")
    ap.add_argument("--tight-cols", default="",
                    help="逗号分隔：连'/'和空白一起去掉的列（硬度 'HB/≤/300'→'HB≤300'、ISO 'P*'→'P'）")
    ap.add_argument("--nospace-cols", default="",
                    help="逗号分隔：只去空白、保留'/'的列（切削领域 '精加工~ 半精加工'→'精加工~半精加工'）")
    ap.add_argument("--require", default="", help="逗号分隔：必须存在的字段，否则跳过该表")
    ap.add_argument("--limits", default="", help="'vc=1:2000,ap=0.01:25'")
    ap.add_argument("--table-filter", default="",
                    help="表头必须包含的关键词（一页多表时用），逗号分隔")
    ap.add_argument("--out", default="主表.csv")
    ap.add_argument("--bad", default="")
    ap.add_argument("--extra", default="", help="逗号分隔：额外保留的字段名（照抄文本）")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    col_map = {}
    for part in args.col_map.split(","):
        k, _, v = part.partition("=")
        if k.strip() and v.strip():
            col_map[v.strip()] = k.strip()
    triplet_fields = {c.strip() for c in args.triplet.split(",") if c.strip()}
    split_fields = {c.strip() for c in args.split.split(",") if c.strip()}
    carry_fields = [c.strip() for c in args.carry.split(",") if c.strip()]
    norm_cols = [c.strip() for c in args.norm_cols.split(",") if c.strip()]
    tight_cols = [c.strip() for c in args.tight_cols.split(",") if c.strip()]
    nospace_cols = [c.strip() for c in args.nospace_cols.split(",") if c.strip()]
    require = [c.strip() for c in args.require.split(",") if c.strip()]
    extra = [c.strip() for c in args.extra.split(",") if c.strip()]
    limits = parse_limits(args.limits)
    tf = [c.strip() for c in args.table_filter.split(",") if c.strip()]

    rng = []
    for part in args.pages.replace(" ", "").split(","):
        if "-" in part:
            a, b = part.split("-", 1)
            rng.extend(range(int(a), int(b) + 1))
        elif part:
            rng.append(int(part))

    # col_map 是 {字段名: 表头关键词}，字段列表要取**键**（取成关键词会让全部身份列变空，
    # 而行数仍然是对的——只看行数会假验收，踩过）
    fields = list(dict.fromkeys(list(col_map.keys()) + extra))
    out_fields = ["page", "table_idx", "src_row"]
    for f in fields:
        if f in triplet_fields:
            out_fields += [f + "_min", f + "_rec", f + "_max"]
        else:
            out_fields.append(f)
    out_fields.append("_err")

    doc = fitz.open(args.pdf)
    ok_rows, bad_rows, stats = [], [], []
    for pno in rng:
        if not (1 <= pno <= doc.page_count):
            print("!! p%d 超出范围" % pno)
            continue
        pg = doc[pno - 1]
        try:
            tables = pg.find_tables().tables
        except AttributeError:
            print("!! PyMuPDF 版本不支持 find_tables()，换无框线列模型")
            return 1
        for ti, t in enumerate(tables):
            rows = t.extract()
            if args.header_rows == "auto":
                nhdr, cols = detect_header_rows(rows, col_map, require,
                                                args.expand, args.max_header_rows)
            else:
                nhdr = int(args.header_rows)
                cols, _ = map_cols(rows[:nhdr], col_map)
            if len(rows) <= nhdr:
                continue
            if any(f not in cols for f in require):
                continue
            if tf:
                hdr = head_key(" ".join(c for r in rows[:nhdr] for c in r if c))
                if not all(k in hdr for k in tf):
                    continue
            param_fields = [f for f in fields if f != args.expand and f in triplet_fields]
            body, n_merged = merge_phantom(rows[nhdr:], cols, param_fields + [args.expand])

            carry = {f: "" for f in carry_fields}
            block, n_data = [], 0
            for ri, r in enumerate(body):
                def get(f):
                    j = cols.get(f)
                    return cell_text(r[j]) if j is not None and j < len(r) else ""

                for f in carry_fields:
                    v = get(f)
                    if v:
                        carry[f] = v

                exp_raw = get(args.expand)
                trips = [parse_triplet(p) for p in
                         [q.strip() for q in exp_raw.split("/") if q.strip()]]
                trips = [x for x in trips if x]
                if not trips:
                    block.append(dict(page=pno, table_idx=ti, src_row=ri,
                                      _err="%s 不是三元组: %r" % (args.expand, exp_raw)))
                    continue
                n = len(trips)
                n_data += 1

                vals = {}
                for f in fields:
                    if f in carry_fields:
                        vals[f] = [carry[f]] * n
                    elif f in split_fields:
                        vals[f] = split_cell(get(f), f, n, triplet_fields)
                    else:
                        vals[f] = [get(f)] * n
                for i in range(n):
                    rec = {"page": pno, "table_idx": ti, "src_row": ri}
                    for f in fields:
                        if f in triplet_fields:
                            tv = parse_triplet(vals[f][i])
                            if tv is None:
                                rec[f + "_min"] = rec[f + "_rec"] = rec[f + "_max"] = ""
                                rec["_err"] = "%s=%r 不是三元组" % (f, vals[f][i])
                            else:
                                rec[f + "_min"], rec[f + "_rec"], rec[f + "_max"] = tv
                        else:
                            v = vals[f][i]
                            if f in norm_cols:
                                v = norm_label(v)
                            elif f in tight_cols:
                                v = norm_tight(v)
                            elif f in nospace_cols:
                                v = norm_nospace(v)
                            rec[f] = v
                    block.append(rec)
                if n == 1:
                    block[-1]["_err"] = ""

            for rec in block:
                if rec.get("_err"):
                    continue
                for k, (lo, hi) in limits.items():
                    for tag in ("min", "rec", "max"):
                        v = rec.get(k + "_" + tag)
                        if v == "" or v is None:
                            continue
                        if v < lo or v > hi:
                            rec["_err"] = "%s_%s=%s 越界" % (k, tag, v)
                if not rec.get("_err") and rec.get(args.expand + "_min"):
                    seq = [rec.get(args.expand + "_" + t) for t in ("min", "rec", "max")]
                    if seq[0] > seq[1]:
                        rec["_err"] = "%s 非单调 %s" % (args.expand, seq)

            # 块级连坐：一个表块有一条不过，整块进低置信
            if any(r.get("_err") for r in block):
                for r in block:
                    r.setdefault("_err", "同块其它行未过校验（块级连坐）")
                bad_rows.extend(block)
            else:
                ok_rows.extend(block)
            stats.append((pno, ti, t.row_count, len(block), n_data, n_merged))
    doc.close()

    def dump(path, rows):
        with io.open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=out_fields, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in out_fields})

    dump(args.out, ok_rows)
    if args.bad:
        dump(args.bad, bad_rows)

    print("页 | 表 | 源行数 | 产出 | 数据行 | 幻影合并")
    for p, ti, nr, nb, nd, nm in stats:
        print("  %-5d %-2d %5d %5d %5d %d" % (p, ti, nr, nb, nd, nm))
    print("\n主表 %d 行 → %s" % (len(ok_rows), args.out))
    print("低置信 %d 行%s" % (len(bad_rows), (" → " + args.bad) if args.bad else "（未指定 --bad，未落盘）"))
    for r in bad_rows[:8]:
        print("  !! p%s 表%s 行%s %s" % (r.get("page"), r.get("table_idx"),
                                        r.get("src_row"), r.get("_err")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
