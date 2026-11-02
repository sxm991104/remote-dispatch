#!/usr/bin/env bash
# remote-dispatch · Linux 安装脚本
#
# 做什么
#     一、检查环境（Python 版本、必要的命令）
#     二、建目录、生成配置模板
#     三、打印"接下来干什么"
#
# 不做什么
#     不动系统包管理器、不装 AstrBot、不改你的 shell 配置。
#     需要装什么会告诉你怎么装，但不会替你做主。

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; DIM=$'\033[2m'; OFF=$'\033[0m'
ok()   { printf '  %s✅%s %s\n' "$GREEN" "$OFF" "$*"; }
warn() { printf '  %s⚠️ %s%s\n' "$YELLOW" "$OFF" "$*"; }
bad()  { printf '  %s❌%s %s\n' "$RED" "$OFF" "$*"; }
note() { printf '  %s%s%s\n' "$DIM" "$*" "$OFF"; }

echo
echo "================================================================"
echo "  remote-dispatch · 安装"
echo "================================================================"
echo

# ── 一、环境检查 ──
echo "【一】环境"

PY=""
for c in python3 python3.13 python3.12 python3.11 python3.10 python; do
    if command -v "$c" >/dev/null 2>&1; then
        v=$("$c" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "0.0")
        major=${v%%.*}; minor=${v##*.}
        if [ "$major" -eq 3 ] && [ "$minor" -ge 10 ]; then
            PY="$c"
            ok "Python $v  ($(command -v "$c"))"
            break
        fi
    fi
done

if [ -z "$PY" ]; then
    bad "没有找到 Python 3.10 或更新的版本"
    echo
    echo "  装一个："
    echo "    Ubuntu/Debian   sudo apt install python3 python3-pip"
    echo "    Fedora          sudo dnf install python3 python3-pip"
    echo "    Arch            sudo pacman -S python python-pip"
    exit 1
fi

if "$PY" -m pip --version >/dev/null 2>&1; then
    ok "pip 可用"
    PIPTIP=""
else
    warn "没有 pip"
    PIPTIP="--break-system-packages"
    echo "    装一下：sudo apt install python3-pip"
fi

# 跑测试要 pytest，跑起来不用
if "$PY" -m pytest --version >/dev/null 2>&1; then
    ok "pytest 可用（跑测试用的）"
else
    note "pytest 没装。跑测试才需要：$PY -m pip install pytest"
fi

# 磁盘
avail=$(df -k "$HERE" 2>/dev/null | awk 'NR==2 {print int($4/1024)}')
if [ -n "${avail:-}" ]; then
    if [ "$avail" -lt 500 ]; then
        warn "只剩 ${avail} MB。任务跑起来会写工作区，建议至少留 500 MB"
    else
        ok "磁盘 ${avail} MB 可用"
    fi
fi

echo

# ── 二、目录和配置 ──
echo "【二】数据目录"

# 跟 Windows 那边保持一致：$XDG_CONFIG_HOME 或 ~/.config 下的 dsh-bridge
CFG="${XDG_CONFIG_HOME:-$HOME/.config}/dsh-bridge"
mkdir -p "$CFG"
ok "配置目录 $CFG"

WORK="$HERE/workspaces"
mkdir -p "$WORK"
ok "工作区目录 $WORK"

# 引擎配置：没有就放一份模板
if [ -f "$CFG/engine.json" ]; then
    ok "engine.json 已存在（没动它）"
else
    cat > "$CFG/engine.json" <<'JSON'
{
  "_说明": [
    "在这里填你的模型。任何 OpenAI 兼容的接口都行：DeepSeek / OpenAI / 通义 / Kimi / 本地 Ollama。",
    "填完之后跑 ./start.sh 就能用。",
    "enabled 为 true 的那几套才会被派活。"
  ],
  "profiles": [
    {
      "id": "p1",
      "name": "我的模型",
      "base_url": "https://api.deepseek.com",
      "api_key": "把 key 填在这里",
      "model": "deepseek-chat",
      "enabled": true,
      "concurrency": 1
    }
  ]
}
JSON
    chmod 600 "$CFG/engine.json"
    ok "生成 engine.json 模板  →  $CFG/engine.json"
    warn "记得把 api_key 填进去"
fi

echo

# ── 三、下一步 ──
echo "【三】接下来干什么"
echo
echo "  1) 填模型"
echo "     \$EDITOR $CFG/engine.json"
echo "     至少要改 api_key 那一行。"
echo
echo "  2) 起服务"
echo "     ./start.sh"
echo
echo "  3) 看看它在干什么"
echo "     ./status.sh"
echo
echo "  4) 跑一遍测试（可选，确认这台的 Python 环境没问题）"
echo "     $PY tests/run_all.py"
echo
echo "  想接聊天软件（飞书 / Telegram / Discord / Slack / 钉钉 / 企微…）"
echo "  需要另装 AstrBot，然后把 plugins/ 下这几个文件拷进它的 plugins 目录："
echo
ls -1 plugins/*.py 2>/dev/null | while read -r f; do note "    $f"; done
echo
note "    AstrBot 的安装方式见 https://docs.astrbot.app"
note "    接好平台之后在 AstrBot 里配「任务端」，不用桌面端。"
echo
echo "================================================================"
echo "  装好了"
echo "================================================================"
echo
