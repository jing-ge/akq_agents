#!/usr/bin/env bash
# AKQ Agents 启动脚本
#
# 用法：
#   ./start.sh                # 启动 web + 自动开 daemon
#   ./start.sh web            # 只启动 web（不开 daemon）
#   ./start.sh stop           # 停止 web + daemon
#   ./start.sh status         # 看当前进程 + daemon 状态
#   ./start.sh logs           # tail web + daemon 日志

set -euo pipefail

# ---- 配置 ----------------------------------------------------------------

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="/opt/anaconda3/envs/akq310/bin/python"
WEB_PORT="${WEB_PORT:-8765}"
WEB_HOST="${WEB_HOST:-127.0.0.1}"

WEB_LOG="$PROJECT_ROOT/data/web.log"
WEB_PID="$PROJECT_ROOT/data/web.pid"
DAEMON_LOG="$PROJECT_ROOT/data/daemon.log"
DAEMON_PID="$PROJECT_ROOT/data/daemon.pid"

cd "$PROJECT_ROOT"
mkdir -p data

export PYTHONPATH="$PROJECT_ROOT/src"

# ---- 工具函数 ------------------------------------------------------------

is_alive() {
    local pid_file="$1"
    [[ -f "$pid_file" ]] || return 1
    local pid
    pid=$(cat "$pid_file" 2>/dev/null || echo "")
    [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

wait_web_ready() {
    for _ in $(seq 1 20); do
        if curl -sf -o /dev/null "http://${WEB_HOST}:${WEB_PORT}/ops"; then
            return 0
        fi
        sleep 1
    done
    return 1
}

# ---- 命令 ---------------------------------------------------------------

cmd_web() {
    if is_alive "$WEB_PID"; then
        echo "[web] 已经在跑，pid=$(cat "$WEB_PID")"
        return 0
    fi
    echo "[web] 启动中... (http://${WEB_HOST}:${WEB_PORT})"
    nohup "$PYTHON" -m akq_agents.cli.app web start \
        --host "$WEB_HOST" --port "$WEB_PORT" \
        > "$WEB_LOG" 2>&1 &
    echo $! > "$WEB_PID"
    if wait_web_ready; then
        echo "[web] 就绪：http://${WEB_HOST}:${WEB_PORT}/ops"
    else
        echo "[web] 启动失败，看日志：$WEB_LOG"
        exit 1
    fi
}

cmd_daemon_start() {
    # 通过 web API 启动，让 daemon 跟 web 注册的 pid 文件保持一致
    if ! is_alive "$WEB_PID"; then
        echo "[daemon] web 未运行，先 ./start.sh web"
        exit 1
    fi
    if is_alive "$DAEMON_PID"; then
        echo "[daemon] 已经在跑，pid=$(cat "$DAEMON_PID")"
        return 0
    fi
    echo "[daemon] 通过 web 启动..."
    curl -sf -X POST "http://${WEB_HOST}:${WEB_PORT}/api/control/daemon/start" \
        | "$PYTHON" -m json.tool || true
    sleep 3
    if is_alive "$DAEMON_PID"; then
        echo "[daemon] 就绪：pid=$(cat "$DAEMON_PID")，日志 $DAEMON_LOG"
    else
        echo "[daemon] 可能仍在启动中，过几秒看 ./start.sh status"
    fi
}

cmd_stop() {
    if is_alive "$WEB_PID"; then
        # 先优雅停 daemon（如果在跑）
        if is_alive "$DAEMON_PID"; then
            echo "[daemon] 停止中..."
            curl -sf -X POST "http://${WEB_HOST}:${WEB_PORT}/api/control/daemon/stop" \
                > /dev/null || true
            sleep 2
        fi
        echo "[web] 停止 pid=$(cat "$WEB_PID")"
        kill "$(cat "$WEB_PID")" 2>/dev/null || true
        sleep 1
        kill -9 "$(cat "$WEB_PID")" 2>/dev/null || true
        rm -f "$WEB_PID"
    else
        echo "[web] 未运行"
    fi
    # 兜底：杀残留的 daemon
    if is_alive "$DAEMON_PID"; then
        echo "[daemon] 强制停止 pid=$(cat "$DAEMON_PID")"
        kill "$(cat "$DAEMON_PID")" 2>/dev/null || true
        sleep 1
        kill -9 "$(cat "$DAEMON_PID")" 2>/dev/null || true
        rm -f "$DAEMON_PID"
    fi
}

cmd_status() {
    if is_alive "$WEB_PID"; then
        echo "[web]    运行中 pid=$(cat "$WEB_PID")  http://${WEB_HOST}:${WEB_PORT}/ops"
    else
        echo "[web]    未运行"
    fi
    if is_alive "$DAEMON_PID"; then
        echo "[daemon] 运行中 pid=$(cat "$DAEMON_PID")"
        curl -sf "http://${WEB_HOST}:${WEB_PORT}/api/ops/health" 2>/dev/null \
            | "$PYTHON" -c "
import json, sys
try:
    d = json.load(sys.stdin)
    daemon = d.get('daemon') or {}
    state = daemon.get('state') or {}
    tb = d.get('today_batch') or {}
    dh = d.get('data_health') or {}
    print(f'         is_alive={daemon.get(\"is_alive\")}  last_heartbeat={state.get(\"last_heartbeat\", \"-\")}')
    print(f'         today_batch.status={tb.get(\"status\", \"-\")}')
    print(f'         data_health={dh.get(\"health\", \"-\")} universe={dh.get(\"universe_size_today\", \"-\")} coverage={dh.get(\"ohlcv_coverage_today\", \"-\")}')
except Exception as e:
    print(f'         (health 查询失败: {e})')
" 2>/dev/null || echo "         (health 查询失败)"
    else
        echo "[daemon] 未运行"
    fi
}

cmd_logs() {
    echo "== tail web.log + daemon.log (Ctrl+C 退出) =="
    tail -f "$WEB_LOG" "$DAEMON_LOG" 2>/dev/null
}

# ---- 入口 ---------------------------------------------------------------

action="${1:-up}"

case "$action" in
    up|"")
        cmd_web
        cmd_daemon_start
        echo
        cmd_status
        echo
        echo "提示：./start.sh logs 看日志  |  ./start.sh stop 停止"
        ;;
    web)
        cmd_web
        ;;
    daemon)
        cmd_daemon_start
        ;;
    stop)
        cmd_stop
        ;;
    status)
        cmd_status
        ;;
    logs)
        cmd_logs
        ;;
    *)
        echo "用法: ./start.sh [up|web|daemon|stop|status|logs]"
        echo "  up      启动 web + daemon（默认）"
        echo "  web     只启动 web"
        echo "  daemon  只启动 daemon（要求 web 在跑）"
        echo "  stop    停止 daemon + web"
        echo "  status  查看运行状态"
        echo "  logs    tail web + daemon 日志"
        exit 1
        ;;
esac
