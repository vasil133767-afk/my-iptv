#!/bin/bash
# Приватний проксі з логіном і паролем на сервері Ubuntu (Oracle Cloud Free тощо).
#   HTTP/HTTPS-проксі (Squid)  — порт 3128: браузери, Windows, більшість програм
#   SOCKS5-проксі (Dante)      — порт 1080: Telegram, торрент-клієнти, скрипти
# Логін і пароль однакові для обох. Повторний запуск зберігає старі логін і пароль;
# щоб згенерувати нові:  sudo rm /opt/proxy/credentials  і запустити знову.
set -e

HTTP_PORT=3128
SOCKS_PORT=1080
DIR=/opt/proxy
CRED=$DIR/credentials

if [ "$(id -u)" != 0 ]; then echo "Запустіть через sudo"; exit 1; fi

echo "==> Встановлюю Squid і Dante"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y -q
apt-get install -y -q squid dante-server openssl

mkdir -p "$DIR"
if [ ! -f "$CRED" ]; then
  PUSER="u$(openssl rand -hex 3)"
  PPASS="$(openssl rand -base64 18 | tr -d '/+=' | cut -c1-16)"
  printf 'USER=%s\nPASS=%s\n' "$PUSER" "$PPASS" > "$CRED"
  chmod 600 "$CRED"
fi
PUSER=$(grep '^USER=' "$CRED" | cut -d= -f2)
PPASS=$(grep '^PASS=' "$CRED" | cut -d= -f2)

echo "==> Обліковий запис проксі"
# системний користувач без входу в систему — його пароль перевіряє Dante
id "$PUSER" >/dev/null 2>&1 || useradd -r -M -s /usr/sbin/nologin "$PUSER"
echo "$PUSER:$PPASS" | chpasswd
# той самий логін і пароль для Squid
printf '%s:%s\n' "$PUSER" "$(openssl passwd -apr1 "$PPASS")" > /etc/squid/passwd
chown proxy:proxy /etc/squid/passwd
chmod 640 /etc/squid/passwd

echo "==> Налаштовую Squid (HTTP, порт $HTTP_PORT)"
cat > /etc/squid/squid.conf <<EOF
http_port $HTTP_PORT
auth_param basic program /usr/lib/squid/basic_ncsa_auth /etc/squid/passwd
auth_param basic realm proxy
auth_param basic credentialsttl 8 hours
acl authed proxy_auth REQUIRED
acl Safe_ports port 80 443 1025-65535
acl CONNECT method CONNECT
http_access deny !Safe_ports
http_access allow authed
http_access deny all
# нічого не кешувати і не видавати, що запит іде через проксі
cache deny all
cache_mem 8 MB
via off
forwarded_for delete
request_header_access X-Forwarded-For deny all
access_log none
cache_log /var/log/squid/cache.log
EOF
systemctl enable -q squid
systemctl restart squid

echo "==> Налаштовую Dante (SOCKS5, порт $SOCKS_PORT)"
EXT_IF=$(ip route get 1.1.1.1 | awk '{for(i=1;i<NF;i++) if($i=="dev") print $(i+1)}' | head -1)
cat > /etc/danted.conf <<EOF
logoutput: syslog
internal: 0.0.0.0 port = $SOCKS_PORT
external: $EXT_IF
socksmethod: username
user.privileged: root
user.unprivileged: nobody
client pass {
  from: 0.0.0.0/0 to: 0.0.0.0/0
}
socks pass {
  from: 0.0.0.0/0 to: 0.0.0.0/0
  command: connect udpassociate
  socksmethod: username
}
EOF
systemctl enable -q danted
systemctl restart danted

echo "==> Відкриваю порти $HTTP_PORT і $SOCKS_PORT у фаєрволі сервера"
for P in $HTTP_PORT $SOCKS_PORT; do
  iptables -C INPUT -p tcp --dport "$P" -j ACCEPT 2>/dev/null || iptables -I INPUT -p tcp --dport "$P" -j ACCEPT
done
iptables -C INPUT -p udp --dport "$SOCKS_PORT" -j ACCEPT 2>/dev/null || iptables -I INPUT -p udp --dport "$SOCKS_PORT" -j ACCEPT
if command -v netfilter-persistent >/dev/null; then netfilter-persistent save >/dev/null 2>&1 || true; fi

sleep 2
IP=$(curl -fsS https://api.ipify.org || echo "IP_СЕРВЕРА")
ok=1
systemctl is-active -q squid || { echo "Squid не запустився:"; journalctl -u squid -n 20 --no-pager; ok=0; }
systemctl is-active -q danted || { echo "Dante не запустився:"; journalctl -u danted -n 20 --no-pager; ok=0; }
[ $ok = 1 ] || exit 1

echo
echo "Готово. Дані проксі:"
echo
echo "    Сервер:  $IP"
echo "    Логін:   $PUSER"
echo "    Пароль:  $PPASS"
echo
echo "    HTTP/HTTPS-проксі:  $IP  порт $HTTP_PORT"
echo "    SOCKS5-проксі:      $IP  порт $SOCKS_PORT"
echo
echo "    Одним рядком:  http://$PUSER:$PPASS@$IP:$HTTP_PORT"
echo "                   socks5://$PUSER:$PPASS@$IP:$SOCKS_PORT"
echo
echo "Не забудьте відкрити порти $HTTP_PORT (TCP) і $SOCKS_PORT (TCP і UDP) у Security List Oracle."
