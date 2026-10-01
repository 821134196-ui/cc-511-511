#!/usr/bin/env bash
# 一条本地启动命令：灌入演示数据（含转科、丢码样例）并启动服务
cd "$(dirname "$0")"
exec python3 app.py "$@"
