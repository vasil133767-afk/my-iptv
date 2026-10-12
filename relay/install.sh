#!/bin/bash
# Встановлення IPTV-ретранслятора на сервер Ubuntu (Oracle Cloud Free тощо).
# Запуск:  curl -fsSL https://raw.githubusercontent.com/vasil133767-afk/my-iptv/main/relay/install.sh | sudo bash
# Повторний запуск оновлює relay.py і зберігає старий ключ та налаштування.
set -e

REPO_RAW="https://raw.githubusercontent.com/vasil133767-afk/my-iptv/main"
DIR=/opt/iptv-relay
PORT=8080

if [ "$(id -u)" != 0 ]; then echo "Запустіть через sudo"; exit 1; fi

echo "==> Встановлюю залежності"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y -q
apt-get install -y -q python3 python3-aiohttp python3-pip curl
pip3 install -q --break-system-packages aiohttp-socks 2>/dev/null || true  # для socks5-проксі

echo "==> Завантажую ретранслятор"
mkdir -p "$DIR"
curl -fsSL "$REPO_RAW/relay/relay.py" -o "$DIR/relay.py"

if [ ! -f "$DIR/env" ]; then
  TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(16))")
  cat > "$DIR/env" <<EOF
TOKEN=$TOKEN
PLAYLIST_URL=$REPO_RAW/playlist.m3u
RELAY_GROUPS=Росія
PORT=$PORT
UPSTREAM_PROXY=
EOF
  chmod 600 "$DIR/env"
fi
PORT=$(grep '^PORT=' "$DIR/env" | cut -d= -f2)
TOKEN=$(grep '^TOKEN=' "$DIR/env" | cut -d= -f2)

echo "==> Налаштовую автозапуск"
cat > /etc/systemd/system/iptv-relay.service <<EOF
[Unit]
Description=IPTV relay
After=network-online.target
Wants=network-online.target

[Service]
EnvironmentFile=$DIR/env
ExecStart=/usr/bin/python3 $DIR/relay.py
Restart=always
RestartSec=5
DynamicUser=yes
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable -q iptv-relay
systemctl restart iptv-relay

echo "==> Відкриваю порт $PORT у фаєрволі сервера"
if ! iptables -C INPUT -p tcp --dport "$PORT" -j ACCEPT 2>/dev/null; then
  iptables -I INPUT -p tcp --dport "$PORT" -j ACCEPT
fi
if command -v netfilter-persistent >/dev/null; then netfilter-persistent save >/dev/null 2>&1 || true; fi
if command -v ufw >/dev/null && ufw status | grep -q active; then ufw allow "$PORT"/tcp >/dev/null; fi

sleep 2
IP=$(curl -fsS https://api.ipify.org || echo "IP_СЕРВЕРА")
if systemctl is-active -q iptv-relay; then
  echo
  echo "Готово. Адреса плейлиста для тюнера:"
  echo
  echo "    http://$IP:$PORT/$TOKEN/playlist.m3u"
  echo
  echo "Нікому не показуйте цю адресу: ключ у ній дає доступ до вашого сервера."
else
  echo "Сервіс не запустився. Лог:"; journalctl -u iptv-relay -n 30 --no-pager; exit 1
fi
