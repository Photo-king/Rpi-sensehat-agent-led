#!/bin/bash
# 在树莓派上安装/更新 sensehat-led 服务
set -euo pipefail

APP_USER="${USER:-linjinle123}"
APP_HOME="$(getent passwd "$APP_USER" | cut -d: -f6)"
APP_DIR="$APP_HOME/sensehat-led"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "==> 安装到 $APP_DIR (用户 $APP_USER)"
mkdir -p "$APP_DIR"
install -m 0755 "$SRC_DIR/sensehat_led.py" "$APP_DIR/sensehat_led.py"
[ -f "$SRC_DIR/README.md" ] && install -m 0644 "$SRC_DIR/README.md" "$APP_DIR/README.md"

echo "==> 写入 systemd 服务"
sudo tee /etc/systemd/system/sensehat-led.service >/dev/null <<UNIT
[Unit]
Description=Sense HAT agent status LED
After=multi-user.target

[Service]
Type=simple
User=$APP_USER
ExecStart=/usr/bin/python3 $APP_DIR/sensehat_led.py --port 8765 -v
Restart=always
RestartSec=2
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable sensehat-led >/dev/null 2>&1 || true
sudo systemctl restart sensehat-led
sleep 1.5

echo "==> 服务状态"
systemctl is-active sensehat-led
echo "==> 本机自检"
curl -sS --max-time 5 http://127.0.0.1:8765/ping || echo "(接口暂时无响应)"
echo
echo "==> 最近日志"
journalctl -u sensehat-led --no-pager -n 12
