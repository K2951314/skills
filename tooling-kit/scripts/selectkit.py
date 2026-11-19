# -*- coding: utf-8 -*-
"""成套配刀启动器 — 定位 smart-selection 并转发 web/selectkit.py。

与 tool-selection 的启动器同一套路：项目定位走 SS_HOME 环境变量 / 常见根
探测，技能里不写任何盘符（写死盘符 = 换机即死，实测踩过）。
selectkit.py 是纯 Python 源码（无 pyc 绑定），解释器 3.10+ 均可，推荐 3.13。

用法:
    py -3.13 tooling-kit/scripts/selectkit.py 外圆 45钢 粗
    py -3.13 tooling-kit/scripts/selectkit.py 攻丝 M8
    py -3.13 tooling-kit/scripts/selectkit.py 内孔 45钢 半精 --dmin 20 --dmax 25 --depth 50
    py -3.13 tooling-kit/scripts/selectkit.py 外圆 45钢 粗 三菱 --json

CLI 契约与原件一致: stdout 文本/JSON + 退出码透传（0=有方案, 1=无方案/失败）。
"""
import os
import subprocess
import sys
from pathlib import Path


def resolve_project_root() -> Path:
    """定位 smart-selection 项目根（判据：web/selectkit.py 存在）。

    语义：显式设置了 SS_HOME 就必须有效（设错直接报错，绝不悄悄改用别处的
    项目）；没设置才按常见根探测。
    """
    def valid(p: Path) -> bool:
        return (p / "web" / "selectkit.py").is_file()

    env = os.environ.get("SS_HOME", "").strip()
    if env:
        cand = Path(env)
        if valid(cand):
            return cand
        raise SystemExit(
            "[SS_HOME 无效] 环境变量 SS_HOME=%s 下找不到 web/selectkit.py。\n"
            "请修正 SS_HOME，或取消设置让脚本自动探测。" % env
        )
    repo_root = Path(__file__).resolve().parents[2]
    candidates = [repo_root.parent / "smart-selection", Path.home() / "smart-selection"]
    for cand in candidates:
        if valid(cand):
            return cand
    tried = "\n  ".join(str(c) for c in candidates)
    raise SystemExit(
        "[项目定位失败] 找不到 smart-selection（判据 web/selectkit.py）。\n"
        "请设置环境变量 SS_HOME 指向项目根目录后重试。已探测:\n  " + tried
    )


def main() -> int:
    root = resolve_project_root()
    target = root / "web" / "selectkit.py"
    proc = subprocess.run([sys.executable, str(target)] + sys.argv[1:])
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())