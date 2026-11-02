#!/usr/bin/env bash
# remote-dispatch · 看状态
#
# 回答三个问题：
#   一、服务在不在跑
#   二、任务在干什么
#   三、有没有卡住的

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; DIM=$'\033[2m'; OFF=$'\033[0m'

PORT="${DSH_BRIDGE_PORT:-8790}"
PIDFILE="$HERE/.bridge.pid"

# ── 一、进程 ──
echo
echo "================================================================"
echo "  服务"
echo "================================================================"

running=0
if [ -f "$PIDFILE" ]; then
    pid=$(cat "$PIDFILE" 2>/dev/null || echo "")
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        echo "  进程      ${GREEN}跑着${OFF}（pid $pid）"
        running=1
    fi
fi

if [ "$running" -eq 0 ]; then
    # 可能是别人手动起的，看端口
    if command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | grep -q ":$PORT "; then
        echo "  进程      ${GREEN}端口 $PORT 有人听${OFF}（不是 ./start.sh 起的）"
        running=1
    elif command -v netstat >/dev/null 2>&1 && netstat -ltn 2>/dev/null | grep -q ":$PORT "; then
        echo "  进程      ${GREEN}端口 $PORT 有人听${OFF}（不是 ./start.sh 起的）"
        running=1
    else
        echo "  进程      ${RED}没在跑${OFF}"
        echo
        echo "  起起来：./start.sh"
        echo
        exit 0
    fi
fi

echo "  端口      $PORT"

# ── 二、配置 ──
CFG="${XDG_CONFIG_HOME:-$HOME/.config}/dsh-bridge"
echo
echo "================================================================"
echo "  配置  ($CFG)"
echo "================================================================"

if [ -f "$CFG/engine.json" ]; then
    echo "  engine.json   有"
    if grep -q '把 key 填在这里' "$CFG/engine.json" 2>/dev/null; then
        echo "                ${YELLOW}⚠️  还是占位 key，派活会失败${OFF}"
    fi
else
    echo "  engine.json   ${RED}没有${OFF} —— 跑 ./setup.sh"
fi

if [ -f "$CFG/guard.json" ]; then
    echo "  guard.json    有（危险操作验证码的配置）"
else
    echo "  guard.json    没有（危险操作会在界面上问，不会发邮件）"
fi

# ── 三、任务 ──
echo
echo "================================================================"
echo "  最近的活"
echo "================================================================"

TOK=""
for t in "${APPDATA:-}/dsh-bridge/api_token.txt" "$CFG/api_token.txt"; do
    [ -f "$t" ] && { TOK=$(cat "$t"); break; }
done

if [ -n "$TOK" ] && command -v curl >/dev/null 2>&1; then
    json=$(curl -s -m 5 -H "X-Token: $TOK" "http://127.0.0.1:$PORT/api/tasks" 2>/dev/null || echo "")
    if [ -n "$json" ] && [ "$json" != "null" ]; then
        if command -v python3 >/dev/null 2>&1; then
            echo "$json" | python3 - <<'PY' 2>/dev/null || echo "  （解析失败，直接看表格）"
import json, sys
try:
    tasks = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if not isinstance(tasks, list):
    sys.exit(0)
if not tasks:
    print("  还没有任务")
    sys.exit(0)

def fmt(t):
    st = t.get("status") or "?"
    mark = {"ok": "✅", "running": "▶️ ", "pending": "⏳", "queued": "⏳",
            "error": "❌", "timeout": "⏱️ ", "cancelled": "⛔"}.get(st, "·")
    name = t.get("name") or ""
    doing = t.get("doing") or ""
    sec = t.get("seconds")
    extra = f"  {sec:.1f}s" if isinstance(sec, (int, float)) else ""
    line = f"  {mark} {name[:28]:28} {st:10}{extra}"
    if doing:
        line += f"  {doing[:30]}"
    return line

for t in tasks[:15]:
    print(fmt(t))
if len(tasks) > 15:
    print(f"  …还有 {len(tasks) - 15} 条")
PY
        else
            echo "  （没有 python3，跳过解析）"
        fi
    else
        echo "  读不到任务列表（token 可能不对，或者服务刚起）"
    fi
else
    echo "  没有 token 文件 —— 服务第一次启动时会生成，再看一次"
fi

echo
