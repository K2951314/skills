# -*- coding: utf-8 -*-
"""一键验收：把所有技能与两条巡检跑一遍，给出可复制的证据。

为什么要有它（2026-11-19）：技能散布在四个 harness、依赖三个外部项目，
「都说能跑」没有意义——要能一条命令复现「哪个技能、跑了什么命令、
退出码多少、输出长什么样」。这也是换机后的第一道体检。

设计口径：
- **只读**：不写业务项目的数据。``quote-document`` 默认在库副本上跑；
  这里显式传 ``--out`` 到临时目录，产物随即清理。
- **正例 + 负例**：只有成功案例证明不了技能会报错。每个技能至少一条
  「应该失败」的用例，且断言的是**退出码语义**而不是报错文案。
- **不改环境**：需要环境变量的（SQ_HOME/SS_HOME）自动探测；探测不到
  标 SKIP 并说明原因，不算失败——换机上没装项目是正常状态。
- **不动事件中台数据**：事件规则只做静态配置校验（不投事件），
  避免往真实库里灌测试数据。

用法::

    py -3.13 tools/verify_all.py                 # 全跑（人读）
    py -3.13 tools/verify_all.py --json          # 机器可读
    py -3.13 tools/verify_all.py --only quote-document,failure-diagnosis
    py -3.13 tools/verify_all.py --with-zkai     # 额外跑 ZK-AI 巡检探针（需联网/上游）

退出码: 0=全通过（SKIP 不算失败）; 1=有失败; 2=脚本自身错误。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

REPO_ROOT = Path(__file__).resolve().parent.parent
SKIP, PASS, FAIL = "SKIP", "PASS", "FAIL"


@dataclass
class Case:
    """一条验收用例：命令 + 期望退出码 + 判据说明。"""

    name: str
    argv: list[str]
    expect: int
    why: str                      # 这条用例在证明什么
    must_contain: str = ""        # 输出里必须出现的串（可选）
    must_not_contain: str = ""
    timeout: int = 180
    env: dict[str, str] = field(default_factory=dict)
    #: 允许多个可接受退出码（用于「上游不可用时按设计失败」这类用例）。
    #: 为空时只看 ``expect``。
    allow_exit: tuple[int, ...] = ()


@dataclass
class Result:
    name: str
    status: str
    detail: str
    seconds: float


def _py() -> str:
    """子进程解释器。

    直接复用当前解释器（不是字面量 ``py -3.13``）：本脚本自己就用 3.13 启动，
    再传 ``-3.13`` 给 ``sys.executable`` 是非法选项（实测直接报
    ``Unknown option: -3``）。版本约束在 ``main`` 入口显式断言，
    免得「用错解释器也能跑过」把问题藏起来。
    """
    return sys.executable


def _assert_interpreter() -> None:
    """业务技能多数绑定 3.13（pyc / 项目依赖）。版本不对要吵，不能假装通过。"""
    if sys.version_info[:2] != (3, 13):
        raise SystemExit(
            "[解释器版本不对] 当前 Python %d.%d。多数技能依赖 3.13。\n"
            "请用: py -3.13 tools/verify_all.py"
            % (sys.version_info[0], sys.version_info[1])
        )


def run_case(case: Case) -> Result:
    env = dict(os.environ)
    env.update(case.env)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    started = time.monotonic()
    try:
        proc = subprocess.run(
            case.argv, capture_output=True, text=True, timeout=case.timeout,
            env=env, encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return Result(case.name, FAIL, "超时 %ss" % case.timeout,
                      time.monotonic() - started)
    except OSError as exc:
        return Result(case.name, FAIL, "启动失败 %s" % exc, time.monotonic() - started)

    elapsed = time.monotonic() - started
    output = (proc.stdout or "") + (proc.stderr or "")
    acceptable = case.allow_exit or (case.expect,)
    if proc.returncode not in acceptable:
        return Result(
            case.name, FAIL,
            "退出码 %s，期望 %s。输出尾部：%s"
            % (proc.returncode, "/".join(str(c) for c in acceptable),
               output.strip()[-400:]),
            elapsed,
        )
    if case.must_contain and case.must_contain not in output:
        return Result(case.name, FAIL,
                      "输出缺少 %r" % case.must_contain, elapsed)
    if case.must_not_contain and case.must_not_contain in output:
        return Result(case.name, FAIL,
                      "输出不该出现 %r" % case.must_not_contain, elapsed)
    first_line = next((ln for ln in output.splitlines() if ln.strip()), "")
    return Result(case.name, PASS, first_line[:160], elapsed)


# --------------------------------------------------------------------------- #
# 环境探测：项目在哪（技能自身的定位逻辑已覆盖，这里只决定能不能跑）
# --------------------------------------------------------------------------- #
def _project(env_name: str, marker: str, dir_name: str) -> Path | None:
    env = os.environ.get(env_name, "").strip()
    candidates = []
    if env:
        candidates.append(Path(env))
    candidates += [REPO_ROOT.parent / dir_name, Path.home() / dir_name]
    for candidate in candidates:
        if (candidate / marker).is_file():
            return candidate
    return None


def _skill(name: str) -> Path:
    return REPO_ROOT / name


def build_cases(with_zkai: bool) -> list[Case]:
    cases: list[Case] = []
    sq = _project("SQ_HOME", "backend/smart_quotation/engine.py", "智能询价")
    ss = _project("SS_HOME", "web/selectkit.py", "smart-selection")
    env_sq = {"SQ_HOME": str(sq)} if sq else {}
    env_ss = {"SS_HOME": str(ss)} if ss else {}

    # ── skill-inventory：可移植性 lint ─────────────────────────────
    cases.append(Case(
        name="skill-inventory/lint",
        argv=[_py(), str(_skill("skill-inventory/scripts/portability_lint.py"))],
        expect=0,
        why="全仓无盘符硬编码 ERROR，且四个 harness 都挂了 junction",
    ))

    # ── quotation-helper ──────────────────────────────────────────
    if sq:
        cases.append(Case(
            name="quotation-helper/报价",
            argv=[_py(), str(_skill("quotation-helper/scripts/quote.py")),
                  "D4*11*50*d4"],
            expect=0,
            why="本地库能查到报价（P0 验收口径）",
            must_contain="18.1",
            env=env_sq, timeout=120,
        ))
        cases.append(Case(
            name="quotation-helper/无命中",
            argv=[_py(), str(_skill("quotation-helper/scripts/quote.py")),
                  "ZZZ_NOT_A_REAL_MODEL"],
            expect=0,
            why="无命中是空列表而不是脚本故障",
            env=env_sq, timeout=120,
        ))
    else:
        cases.append(Case("quotation-helper", ["cmd", "/c", "echo", "skip"],
                          -999, "未找到智能询价项目"))

    # ── tool-selection ────────────────────────────────────────────
    if ss:
        cases.append(Case(
            name="tool-selection/替代",
            argv=[_py(), str(_skill("tool-selection/scripts/substitute.py")),
                  "WNMG080408-MA"],
            expect=0, why="跨品牌替代查询能出刀杆与牌号对照",
            env=env_ss, timeout=180,
        ))
    # ── tooling-kit ───────────────────────────────────────────────
    if ss:
        cases.append(Case(
            name="tooling-kit/成套配刀",
            argv=[_py(), str(_skill("tooling-kit/scripts/selectkit.py")),
                  "外圆", "45钢", "粗"],
            expect=0, why="五项互锁全 √ 才输出方案",
            must_contain="互锁", env=env_ss, timeout=180,
        ))
        cases.append(Case(
            name="tooling-kit/切槽不造假配对",
            argv=[_py(), str(_skill("tooling-kit/scripts/selectkit.py")),
                  "切槽", "45钢", "粗"],
            expect=1, why="专用刀片未收录时必须报缺，不能给假配对",
            env=env_ss, timeout=180,
        ))

    # ── failure-diagnosis ─────────────────────────────────────────
    if ss:
        cases.append(Case(
            name="failure-diagnosis/崩刃铣削",
            argv=[_py(), str(_skill("failure-diagnosis/scripts/failures.py")),
                  "崩刃", "--op", "铣削"],
            expect=0, why="31 句/6 品牌且带综合册对策（P1 验收口径）",
            must_contain="综合", env=env_ss, timeout=120,
        ))
        cases.append(Case(
            name="failure-diagnosis/无命中",
            argv=[_py(), str(_skill("failure-diagnosis/scripts/failures.py")),
                  "绝不存在的失效现象"],
            expect=1, why="退出码 1 = 无命中（正常语义），不是脚本故障",
            env=env_ss, timeout=120,
        ))

    # ── quote-document ────────────────────────────────────────────
    if sq:
        # 产物必须落在本仓库下再清理。用裸 mkdtemp 会跟着**当前工作目录**
        # 漂到别的项目里（实测落在 ZK-AI 的 .tmp/ 下，且不会自动删）。
        out_root = REPO_ROOT / ".verify-out"
        out_root.mkdir(parents=True, exist_ok=True)
        temp_out = Path(tempfile.mkdtemp(prefix="doc-", dir=str(out_root)))
        cases.append(Case(
            name="quote-document/自检",
            argv=[_py(), str(_skill("quote-document/scripts/quote_document.py")),
                  "--check"],
            expect=0, why="项目定位 + 依赖 + 导出能力全就绪",
            env=env_sq, timeout=120,
        ))
        cases.append(Case(
            name="quote-document/生成 docx",
            argv=[_py(), str(_skill("quote-document/scripts/quote_document.py")),
                  "D4*11*50*d4", "--qty", "3", "--buyer", "验收买方",
                  "--number", "VERIFY-001", "--format", "docx",
                  "--out", str(temp_out)],
            # 环境变量由调用方在 finally 里清理
            expect=0, why="询价 → 单据全链路；默认走库副本不占真实序列",
            must_contain="单据已生成", env=env_sq, timeout=180,
        ))
        cases.append(Case(
            name="quote-document/骑缝章缺主章",
            argv=[_py(), str(_skill("quote-document/scripts/quote_document.py")),
                  "D4*11*50*d4", "--buyer", "验收买方", "--number", "VERIFY-002",
                  "--format", "pdf", "--seam-seal", "--out", str(temp_out)],
            expect=4, why="必须显式报错，不能静默降级成不盖章",
            env=env_sq, timeout=120,
        ))
        cases.append(Case(
            name="quote-document/无命中",
            argv=[_py(), str(_skill("quote-document/scripts/quote_document.py")),
                  "ZZZ_NOT_A_REAL_MODEL", "--buyer", "验收买方", "--number", "VERIFY-003",
                  "--out", str(temp_out)],
            expect=1, why="型号查不到时退出码 1，与「导出失败」区分开",
            env=env_sq, timeout=120,
        ))
        # 公司不存在必须给中文错误，不能吐 Python 堆栈（2026-11-19 修）
        cases.append(Case(
            name="quote-document/公司不存在",
            argv=[_py(), str(_skill("quote-document/scripts/quote_document.py")),
                  "D4*11*50*d4", "--buyer", "验收买方", "--number", "VERIFY-004",
                  "--company", "definitely-no-such-company", "--out", str(temp_out)],
            expect=2, why="公司 ID 错时给中文定位错误（退出码 2），不是未捕获异常",
            must_contain="公司不可用",
            must_not_contain="Traceback",
            env=env_sq, timeout=120,
        ))
        # 同名单据不能静默覆盖（2026-11-19 修）：预置一个同名文件再生成，
        # 判据是「原文件字节不变 + 出现改存提示」，而不是只看退出码。
        dup_dir = Path(tempfile.mkdtemp(prefix="dup-", dir=str(out_root)))
        (dup_dir / "报价单_默认_VERIFY-005.docx").write_bytes(b"PRE-EXISTING")
        cases.append(Case(
            name="quote-document/同名不覆盖",
            argv=[_py(), str(_skill("quote-document/scripts/quote_document.py")),
                  "D4*11*50*d4", "--buyer", "验收买方", "--number", "VERIFY-005",
                  "--out", str(dup_dir)],
            expect=0, why="同名单据改存 -2 并提示，不静默覆盖财务文件",
            must_contain="同名文件已存在",
            env=env_sq, timeout=120,
        ))
        # --locale 必须真的影响产物：初版收了参数却没放进请求（静默无效），
        # 判据只能是「文件名的语言前缀」——退出码恒为 0，看不出真假。
        cases.append(Case(
            name="quote-document/locale 生效",
            argv=[_py(), str(_skill("quote-document/scripts/quote_document.py")),
                  "D4*11*50*d4", "--buyer", "Test Buyer", "--number", "VERIFY-006",
                  "--format", "docx", "--locale", "en", "--out", str(temp_out)],
            expect=0, why="--locale en 出英文文件名（Quote_…），不是中文（报价单_…）",
            must_contain="Quote_",
            must_not_contain="报价单_默认_VERIFY-006",
            env=env_sq, timeout=120,
        ))
    # ── mitsubishi-inventory ──────────────────────────────────────
    if sq:
        # 这条**不是**「应该成功」：上游 401 时技能按设计返回退出码 3 +
        # 结构化 fail-closed 错误。验收要的是「失败得可说」——有 ok:false、
        # 有可操作 hint，而不是崩栈或悄悄给可疑数字。上游恢复时退出码 0。
        # 所以两种退出码都算通过，靠 must_contain="diagnostics" 兜住结构。
        cases.append(Case(
            name="mitsubishi-inventory/fail-closed 结构化",
            argv=[_py(), str(_skill("mitsubishi-inventory/scripts/stock.py")),
                  "WNMG080408-MA", "UC5115"],
            expect=0, allow_exit=(0, 3),
            why="401 时 fail-closed 返回结构化错误（退出码 3）；恢复时返回库存（0）",
            must_contain="diagnostics", env=env_sq, timeout=180,
        ))

    # ── ZK-AI 侧（可选）──────────────────────────────────────────
    if with_zkai:
        zkai = REPO_ROOT.parent / "AI" / "ZK-AI"
        if (zkai / "scripts" / "check_config.py").is_file():
            py = zkai / ".venv" / "Scripts" / "python.exe"
            interpreter = str(py) if py.is_file() else _py()
            cases.append(Case(
                name="zkai/配置体检（含巡检规则）",
                argv=[interpreter, str(zkai / "scripts" / "check_config.py")],
                expect=0, why="事件规则引用的 skill/角色存在，类型有产出方",
                timeout=240,
            ))
            if ss:
                cases.append(Case(
                    name="zkai/数据新鲜度探针",
                    argv=[interpreter, "-c",
                          "import sys,json;sys.path.insert(0,r'%s');"
                          "import signal_probes as p;"
                          "print(json.dumps(p.probe_freshness(p.resolve_ss_home()),ensure_ascii=False))"
                          % (zkai / "scripts")],
                    expect=0, why="探针能跑（ok=false 也算跑通：退出码 0 表示探针本身正常）",
                    env={"SS_HOME": str(ss)}, timeout=300,
                ))
    return cases


def _check_preserved_files() -> list[Result]:
    """验收里预置的哨兵文件必须原样还在——证明「不覆盖」是真生效，不只是提示了。

    这类判据只能这样测：只断言输出里有「改存为」不够，脚本完全可以既提示
    又覆盖。所以预置一段已知字节，跑完比对它没被写。
    """
    out_root = REPO_ROOT / ".verify-out"
    if not out_root.is_dir():
        return []
    sentinels = list(out_root.glob("dup-*/报价单_默认_VERIFY-005.docx"))
    if not sentinels:
        return []
    intact = all(p.read_bytes() == b"PRE-EXISTING" for p in sentinels)
    return [Result(
        "quote-document/原文件未被覆盖",
        PASS if intact else FAIL,
        "预置哨兵字节未被改动" if intact else "预置文件被覆盖了（不覆盖逻辑没生效）",
        0.0,
    )]


def _cleanup_outputs() -> None:
    """清掉本次验收的产物目录（仅限本脚本自己的 ``.verify-out``）。"""
    out_root = REPO_ROOT / ".verify-out"
    if out_root.is_dir():
        shutil.rmtree(out_root, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="my-skills 一键验收")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    ap.add_argument("--only", help="只跑匹配的技能前缀，逗号分隔")
    ap.add_argument("--with-zkai", action="store_true", help="额外跑 ZK-AI 巡检探针")
    args = ap.parse_args()

    cases = build_cases(args.with_zkai)
    # 占位用例（缺项目那种）不执行，直接记 SKIP
    skipped = [c for c in cases if c.expect == -999]
    cases = [c for c in cases if c.expect != -999]
    if args.only:
        prefixes = tuple(p.strip() for p in args.only.split(",") if p.strip())
        cases = [c for c in cases if c.name.startswith(prefixes)]

    results: list[Result] = []
    for case in cases:
        result = run_case(case)
        results.append(result)
        mark = {PASS: "✓", FAIL: "✗", SKIP: "-"}[result.status]
        print("%s %-46s %5.1fs  %s" % (mark, case.name, result.seconds, result.detail),
              flush=True)

    # 哨兵检查只在「同名不覆盖」那条用例真的跑过时才有意义。
    # 不加这个判断的话，`--only skill-inventory` 也会报一条「原文件未被覆盖」
    # 通过——那条用例根本没执行，是纯粹的假绿灯（2026-11-20 实测）。
    extra: list[Result] = []
    if not args.only or any(c.name.startswith("quote-document") for c in cases):
        extra = _check_preserved_files()
    for result in extra:
        mark = {PASS: "✓", FAIL: "✗", SKIP: "-"}[result.status]
        print("%s %-46s %5.1fs  %s" % (mark, result.name, result.seconds, result.detail),
              flush=True)
    results.extend(extra)

    for case in skipped:
        results.append(Result(case.name, SKIP, case.why, 0.0))
        if not args.only:
            print("- %-46s %5s  %s" % (case.name, "-", case.why))

    failed = [r for r in results if r.status == FAIL]
    passed = [r for r in results if r.status == PASS]
    skipped_results = [r for r in results if r.status == SKIP]

    print()
    print("=" * 78)
    print("通过 %d / 失败 %d / 跳过 %d" % (len(passed), len(failed), len(skipped_results)))
    if skipped_results:
        print("跳过的原因（不是失败）：")
        for result in skipped_results:
            print("  - %s：%s" % (result.name, result.detail))
    if failed:
        print("失败项需要立刻看，不要带着红跑其他改动。")
    print("=" * 78)

    if args.json:
        print(json.dumps(
            {"results": [
                {"name": r.name, "status": r.status,
                 "detail": r.detail, "seconds": round(r.seconds, 2)}
                for r in results
            ],
             "passed": len(passed), "failed": len(failed), "skipped": len(skipped_results)},
            ensure_ascii=False, indent=2))
    _cleanup_outputs()
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        _assert_interpreter()
        sys.exit(main())
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        sys.exit(130)
