#!/usr/bin/env bash
# 在全新 Ubuntu 22.04/24.04 VPS 上，以 root 或 sudo 执行（需先把项目放到 /opt/quant-monitor）
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/quant-monitor}"
PY="${PY:-python3}"

echo "==> 系统依赖"
apt-get update
apt-get install -y python3 python3-venv python3-pip nginx git

echo "==> 应用目录: $APP_DIR"
if [ ! -f "$APP_DIR/app/main.py" ]; then
  echo "错误: 请先将 quant-monitor 代码放到 $APP_DIR（git clone 或 scp）"
  exit 1
fi

cd "$APP_DIR"
$PY -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -r requirements.txt

mkdir -p data
chown -R www-data:www-data "$APP_DIR/data" 2>/dev/null || true

if [ ! -f .env ]; then
  cp deploy/.env.production.example .env
  echo "已生成 .env，请编辑 API_KEY: nano $APP_DIR/.env"
fi

echo "==> systemd"
cp deploy/quant-monitor.service /etc/systemd/system/quant-monitor.service
systemctl daemon-reload
systemctl enable quant-monitor
systemctl restart quant-monitor

echo "==> nginx"
cp deploy/nginx-quant-monitor.conf /etc/nginx/sites-available/quant-monitor
ln -sf /etc/nginx/sites-available/quant-monitor /etc/nginx/sites-enabled/quant-monitor
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

echo ""
echo "完成。请："
echo "  1) 编辑 $APP_DIR/.env 设置 API_KEY"
echo "  2) 编辑 /etc/nginx/sites-available/quant-monitor 中的 server_name"
echo "  3) systemctl restart quant-monitor"
echo "  4) 浏览器打开 http://你的域名或IP/ui ，在 ① 保存 API Key"
echo "  5) 建议: certbot --nginx -d 你的域名"
