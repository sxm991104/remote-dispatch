#!/usr/bin/env bash
# remote-dispatch · 起服务
#
#   前台起（Ctrl+C 停）：   ./start.sh
#   后台起：               ./start.sh --daemon
#   停掉：                 ./start.sh --stop
#
# 只起接口服务，不起 AstrBot。
# AstrBot 是聊天那一层，它自己有起法，见 README。

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; OFF=$'\033[0m'
ok()   { printf '  %s✅%s %s\n' "$GREEN" "$OFF" "$*"; }
warn() { printf '  %s⚠️ %s%s\n' "$YELLOW" "$OFF" "$*"; }
bad()  { printf '  %s❌%s %s\n' "$RED" "$OFF" "$*"; }

PY=""
for c in python3 python3.13 python3.12 python3.11 python3.10 python; do
    if command -v "$c" >/dev/null 2>&1; then
        v=$("$c" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "0.0")
        major=${v%%.*}; minor=${v##*.}
        if [ "$major" -eq 3 ] && [ "$minor" -ge 10 ]; then PY="$c"; break; fi
    fi
done
if [ -z "$PY" ]; then
    bad "没有 Python 3.10+。先跑 ./setup.sh"
    exit 1
fi

PORT="${DSH_BRIDGE_PORT:-8790}"
PIDFILE="$HERE/.bridge.pid"
LOGFILE="$HERE/bridge.log"

is_running() {
    [ -f "$PIDFILE" ] || return 1
    pid=$(cat "$PIDFILE" 2>/dev/null || echo "")
    [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null
}

case "${1:-}" in
    --stop)
        if is_running; then
            pid=$(cat "$PIDFILE")
            kill "$pid" 2>/dev/null || true
            for _ in 1 2 3 4 5 6 7 8 9 10; do
                is_running || break
                sleep 0.3
            done
            if is_running; then
                kill -9 "$pid" 2>/dev/null || true
                warn "强杀了（它没在 3 秒内退出）"
            else
                ok "已停"
            fi
            rm -f "$PIDFILE"
        else
            warn "没在跑"
        fi
        exit 0
        ;;
esac

if is_running; then
    pid=$(cat "$PIDFILE")
    ok "已经在跑了（pid $pid）"
    echo "     看状态：./status.sh"
    echo "     停掉：  ./start.sh --stop"
    exit 0
fi

if [ ! -f server/api_server.py ]; then
    bad "找不到 server/api_server.py —— 是不是文件不全？"
    exit 1
fi

# 配置检查：没配模型就提醒一下，但照起
CFG="${XDG_CONFIG_HOME:-$HOME/.config}/dsh-bridge/engine.json"
if [ -f "$CFG" ]; then
    if grep -q '把 key 填在这里' "$CFG" 2>/dev/null; then
        warn "engine.json 里还是模板的占位 key —— 派活会跑不起来"
        echo "     \$EDITOR $CFG"
    fi
else
    warn "还没有 engine.json —— 先跑 ./setup.sh"
fi

export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1

if [ "${1:-}" = "--daemon" ]; then
    nohup "$PY" server/api_server.py --port "$PORT" > "$LOGFILE" 2>&1 &
    echo $! > "$PIDFILE"
    sleep 1.5
    if is_running; then
        ok "在后台跑起来了（pid $(cat "$PIDFILE")，端口 $PORT）"
        echo "     日志：$LOGFILE"
        echo "     状态：./status.sh"
        echo "     停掉：./start.sh --stop"
    else
        bad "起不来，看日志：$LOGFILE"
        tail -20 "$LOGFILE" 2>/dev/null || true
        rm -f "$PIDFILE"
        exit 1
    fi
else
    ok "前台启动（Ctrl+C 停）"
    echo "     端口 $PORT，只接受本机连接"
    echo
    exec "$PY" server/api_server.py --port "$PORT"
fi
