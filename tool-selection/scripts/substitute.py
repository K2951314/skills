# -*- coding: utf-8 -*-
"""跨品牌替代查询启动器 — 定位 smart-selection 并转发 web/substitute.py。

为什么要这层包装（两条都是实测踩过的坑）:
- 项目定位走 SS_HOME 环境变量 / 常见根探测，技能里不写任何盘符。
  写死盘符 = 换机即死（旧机器路径曾让本技能整条失效）。
- substitute 的真实实现是 3.13 的 pyc：解释器版本不对会**静默返回空**
  （exit 0 无输出，极易误判成「没有替代品」）。这里先验版本、再验
  pycbin 存在，把静默失败变成响亮的中文报错。

用法（必须用 Python 3.13 跑本包装）:
    py -3.13 tool-selection/scripts/substitute.py WNMG080408-MA
    py -3.13 tool-selection/scripts/substitute.py "型号1
型号2"

CLI 契约与原件一致: stdout 文本 + 退出码透传（web/src/server.py 也按此调用）。
"""
import os
import subprocess
import sys
from pathlib import Path


def resolve_project_root() -> Path:
    """定位 smart-selection 项目根（判据：web/substitute.py 存在）。

    语义：显式设置了 SS_HOME 就必须有效（设错直接报错，绝不悄悄改用别处的
    项目）；没设置才按常见根探测。
    """
    def valid(p: Path) -> bool:
        return (p / "web" / "substitute.py").is_file()

    env = os.environ.get("SS_HOME", "").strip()
    if env:
        cand = Path(env)
        if valid(cand):
            return cand
        raise SystemExit(
            "[SS_HOME 无效] 环境变量 SS_HOME=%s 下找不到 web/substitute.py。\n"
            "请修正 SS_HOME，或取消设置让脚本自动探测。" % env
        )
    # 技能仓库（my-skills）与项目通常在同一父目录下
    repo_root = Path(__file__).resolve().parents[2]
    candidates = [repo_root.parent / "smart-selection", Path.home() / "smart-selection"]
    for cand in candidates:
        if valid(cand):
            return cand
    tried = "\n  ".join(str(c) for c in candidates)
    raise SystemExit(
        "[项目定位失败] 找不到 smart-selection（判据 web/substitute.py）。\n"
        "请设置环境变量 SS_HOME 指向项目根目录后重试。已探测:\n  " + tried
    )


def main() -> int:
    if sys.version_info[:2] != (3, 13):
        print(
            "[解释器版本错误] 当前 Python %d.%d。substitute 的 pyc 只认 3.13，"
            "其它版本会静默返回空（不报错）。\n请改用: py -3.13 \"%s\" %s"
            % (sys.version_info[0], sys.version_info[1],
               Path(__file__).resolve(), " ".join(sys.argv[1:])),
            file=sys.stderr,
        )
        return 2

    root = resolve_project_root()

    pycbin = root / "web" / "_vendor" / "substitute_orig.cpython-313.pycbin"
    if not pycbin.is_file():
        print(
            "[pycbin 缺失] %s\n"
            "它是 substitute 的唯一实现副本（原 .py 源码已误删），被 .gitignore 排除，"
            "git clone 不会带上它。\n换机时必须用 one-click-migrate 从旧机搬回 "
            "web/_vendor/ 目录；找回前替代查询不可用。" % pycbin,
            file=sys.stderr,
        )
        return 3

    target = root / "web" / "substitute.py"
    proc = subprocess.run([sys.executable, str(target)] + sys.argv[1:])
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())