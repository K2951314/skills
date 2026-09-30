#!/usr/bin/env bash
# 把本仓库挂载到 WorkBuddy AI 的技能扫描目录（整仓一个链接）
# 用法：bash scripts/link-skills.sh [目标skills目录]
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAME="$(basename "$REPO")"
TARGET_ROOT="${1:-$HOME/.workbuddy-ai/skills}"
TARGET="$TARGET_ROOT/$NAME"

mkdir -p "$TARGET_ROOT"

if [ -e "$TARGET" ] || [ -L "$TARGET" ]; then
  echo "[跳过] 已存在：$TARGET"
else
  case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*)
      # Windows：用 junction，避免 MSYS 软链被拷贝成实体目录
      WT="$(cygpath -w "$TARGET")"
      WR="$(cygpath -w "$REPO")"
      powershell.exe -NoProfile -Command "New-Item -ItemType Junction -Path '$WT' -Target '$WR' | Out-Null"
      ;;
    *)
      ln -s "$REPO" "$TARGET"
      ;;
  esac
  echo "[完成] 已挂载"
  echo "       链接：$TARGET"
  echo "       指向：$REPO"
fi

echo ""
echo "本仓库已注册的技能："
found=0
for d in "$REPO"/*/; do
  if [ -f "$d/SKILL.md" ]; then
    echo "  - $(basename "$d")"
    found=1
  fi
done
[ "$found" -eq 0 ] && echo "  （暂无，按 README 新增技能后无需重新挂载）"

echo ""
echo "提示：新增技能后无需重新挂载；改过 description 需新开会话才生效。"
