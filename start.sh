#!/usr/bin/env bash
# 蒸馏:启动上传服务(8000)+ WebDAV(8082)
# 用法:./start.sh   |   ./stop.sh
cd "$(dirname "$0")"
mkdir -p logs
PY=./.venv/bin/python

[ -f logs/upload.pid ] && kill "$(cat logs/upload.pid)" 2>/dev/null
[ -f logs/webdav.pid ] && kill "$(cat logs/webdav.pid)" 2>/dev/null
sleep 1

nohup "$PY" upload.py >> logs/upload.log 2>&1 &
echo $! > logs/upload.pid

nohup "$PY" webdav.py >> logs/webdav.log 2>&1 &
echo $! > logs/webdav.pid

sleep 1
IP=$(hostname -I | tr ' ' '\n' | grep -v '^100\.' | grep -v '^127\.' | head -1)
echo "已启动:"
echo "  控制台    http://$IP:8000"
echo "  WebDAV     http://$IP:8082  (用户/密码见 config.yaml)"
echo "  日志: logs/upload.log, logs/webdav.log"
echo "  如需公网访问:自行配置反向代理或 Tunnel(见 docs/决策记录.md)"
