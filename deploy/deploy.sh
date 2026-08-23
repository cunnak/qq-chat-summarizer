#!/usr/bin/env bash
# qq-chat-summarizer 云服务器一键部署 (Ubuntu/Debian/CentOS)
# 用法: sudo bash deploy.sh
set -e

APP_NAME="qq-chat-summarizer"
INSTALL_DIR="/opt/qq-chat-summarizer"
SERVICE="qq-summarizer"
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"

[ "$(id -u)" = "0" ] || { echo "请用 sudo 运行"; exit 1; }
echo ">>> 部署 $APP_NAME 到 $INSTALL_DIR"

# 1. Python 环境
if command -v python3 >/dev/null 2>&1 && python3 -c "import sys; assert sys.version_info >= (3, 9)" 2>/dev/null; then
    PYTHON="python3"
else
    echo ">>> 安装 Python3..."
    if command -v apt-get >/dev/null 2>&1; then
        apt-get update -qq && apt-get install -y -qq python3 python3-venv python3-pip
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y python3
    elif command -v yum >/dev/null 2>&1; then
        yum install -y python3
    else
        echo "!! 无法识别包管理器, 请手动安装 Python3.9+"; exit 1
    fi
    PYTHON="python3"
fi

# 2. 安装目录与 venv
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"
if [ ! -d .venv ]; then
    echo ">>> 创建虚拟环境..."
    "$PYTHON" -m venv .venv || { "$PYTHON" -m pip install -q virtualenv && "$PYTHON" -m virtualenv .venv; }
fi

# 3. 复制代码
echo ">>> 复制代码..."
cp -f "$SRC_DIR"/*.py "$INSTALL_DIR/"
cp -f "$SRC_DIR"/webui.html "$INSTALL_DIR/"
cp -f "$SRC_DIR"/config.example.json "$INSTALL_DIR/"
[ -f "$SRC_DIR/requirements.txt" ] && cp -f "$SRC_DIR/requirements.txt" "$INSTALL_DIR/"

# 4. 依赖
echo ">>> 安装依赖..."
./.venv/bin/pip install -q -U pip
./.venv/bin/pip install -q -r requirements.txt

# 5. 初始化配置(保留已有配置)
if [ ! -f "$INSTALL_DIR/config.json" ]; then
    cp "$INSTALL_DIR/config.example.json" "$INSTALL_DIR/config.json"
    echo ">>> 已生成默认配置 config.json (控制台里完成具体配置)"
fi

# 6. systemd 服务
echo ">>> 注册 systemd 服务..."
cat > /etc/systemd/system/${SERVICE}.service <<EOF
[Unit]
Description=QQ Chat Summarizer
After=network.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
Environment=QQCS_SYSTEMD=1
ExecStart=$INSTALL_DIR/.venv/bin/python $INSTALL_DIR/main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1
systemctl restart "$SERVICE"

sleep 3
echo
echo "================ 部署完成 ================"
systemctl --no-pager -l status "$SERVICE" | head -8 || true
echo
echo "控制台地址: http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo 服务器IP):8787"
echo "  - 首次访问请设置控制台密码, 然后在向导中完成 LLM/群配置"
echo "  - 云服务器请在安全组/防火墙放行 8787 端口"
echo "常用命令:"
echo "  systemctl status $SERVICE     # 查看状态"
echo "  journalctl -u $SERVICE -n 50  # 查看日志"
echo "  systemctl restart $SERVICE    # 重启"
