#!/bin/bash
# 自动重连的 localhost.run SSH 隧道
# 断线后自动重新连接，每次打印新 URL

while true; do
    echo "[$(date '+%H:%M:%S')] 连接隧道..."
    ssh -o StrictHostKeyChecking=no \
        -o ServerAliveInterval=10 \
        -o ServerAliveCountMax=6 \
        -o ExitOnForwardFailure=yes \
        -R 80:localhost:8000 nokey@localhost.run 2>&1 | grep -E "lhr.life|error|Error"
    echo "[$(date '+%H:%M:%S')] 隧道断开，5秒后重连..."
    sleep 5
done
