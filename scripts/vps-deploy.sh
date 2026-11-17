#!/usr/bin/env bash
# Idempotent VPS provisioning + deploy (Ubuntu). Run as root; used by deploy-backend.yml.
# Env (optional, from CI secrets): DATABASE_URL, R2_*, EUMETSAT_CONSUMER_KEY/SECRET
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker
fi

# Swap: required on 1 GB instances (image build + pySTEPS peaks)
if ! swapon --show | grep -q /swapfile; then
  [ -f /swapfile ] || { fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile; }
  swapon /swapfile
  grep -q /swapfile /etc/fstab || echo "/swapfile none swap sw 0 0" >> /etc/fstab
fi

# Firewall: SSH + HTTP(S). Oracle images REJECT everything else in iptables.
if [ -f /etc/iptables/rules.v4 ] && grep -q REJECT /etc/iptables/rules.v4; then
  for port in 80 443; do
    iptables -C INPUT -p tcp --dport $port -j ACCEPT 2>/dev/null \
      || iptables -I INPUT 5 -p tcp -m state --state NEW --dport $port -j ACCEPT
  done
  command -v netfilter-persistent >/dev/null && netfilter-persistent save >/dev/null
elif command -v ufw >/dev/null && ufw status | grep -q active; then
  ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
fi

touch .env && chmod 600 .env
setenv() { grep -q "^$1=" .env && sed -i "s#^$1=.*#$1=$2#" .env || echo "$1=$2" >> .env; }
for k in DATABASE_URL R2_ACCOUNT_ID R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY EUMETSAT_CONSUMER_KEY EUMETSAT_CONSUMER_SECRET; do
  v="${!k:-}"; [ -n "$v" ] && setenv "$k" "$v"
done
grep -q '^DATABASE_URL=.\+' .env || { echo "DATABASE_URL missing (set the GitHub secret)"; exit 1; }

docker compose -f docker-compose.vps.yml up -d --build --remove-orphans
docker image prune -f >/dev/null
docker compose -f docker-compose.vps.yml ps
