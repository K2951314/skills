#!/usr/bin/env bash
# 把本仓库挂载到各 AI 工具的技能扫描目录（每个目标一个整仓 junction / symlink）。
# 工具不同、扫描目录也不同：
#   WorkBuddy AI : ~/.workbuddy-ai/skills
#   Codex        : ~/.codex/skills
#   Claude Code  : ~/.claude/skills
# 用法：
#   bash scripts/link-skills.sh                                  # 三个目标都挂
#   bash scripts/link-skills.sh "$HOME/.codex/skills"            # 只挂一个
#   bash scripts/link-skills.sh "$HOME/.codex/skills" "$HOME/.claude/skills"
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAME="$(basename "$REPO")"

if [ "$#" -gt 0 ]; then
  TARGET_ROOTS=("$@")
else
  TARGET_ROOTS=(
    "$HOME/.workbuddy-ai/skills"
    "$HOME/.codex/skills"
    "$HOME/.claude/skills"
  )
fi

for TARGET_ROOT in "${TARGET_ROOTS[@]}"; do
  TARGET="$TARGET_ROOT/$NAME"
  mkdir -p "$TARGET_ROOT"

  if [ -e "$TARGET" ] || [ -L "$TARGET" ]; then
    echo "[跳过] 已存在：$TARGET"
    continue
  fi

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
done

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