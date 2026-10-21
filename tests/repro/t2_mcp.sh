# ② MCP 서브프로세스가 멈추거나(SIGSTOP) 죽었을 때(SIGKILL)
TS="$(cd "$(dirname "$0")" && pwd)"; R="$(cd "$TS/../.." && pwd)"
mcp_pid() { pgrep -f "mcp_server.py" | head -1; }
P=$(mcp_pid); echo "  MCP pid=$P → SIGSTOP(멈춤)"; kill -STOP $P
MAXT=20 bash $TS/chat.sh "OrderService.cancel 호출 흐름" | sed 's/^/  /'
kill -CONT $P 2>/dev/null
P=$(mcp_pid); echo "  MCP pid=$P → SIGKILL(죽음)"; kill -9 $P; sleep 1
MAXT=20 bash $TS/chat.sh "OrderService.cancel 호출 흐름" | sed 's/^/  /'
echo "  5초 뒤 다시:"; sleep 5
MAXT=20 bash $TS/chat.sh "OrderService.cancel 호출 흐름" | sed 's/^/  /'
echo "  MCP 프로세스: $(pgrep -f mcp_server.py | wc -l)개"
