#!/usr/bin/env bash
# 蒸馏:停止所有服务(含 Cloudflare Tunnel)
cd "$(dirname "$0")"
for f in logs/upload.pid logs/webdav.pid logs/cloudflared.pid; do
  [ -f "$f" ] && kill "$(cat "$f")" 2>/dev/null && rm -f "$f"
done
sleep 1
# cloudflared 可能留下子进程,兜底清理
pkill -x cloudflared 2>/dev/null
echo "已停止"
