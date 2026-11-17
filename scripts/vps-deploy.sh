#!/usr/bin/env bash
# Idempotent VPS provisioning + deploy (Debian/Ubuntu). Run as root from /opt/nowcast.
# Used by .github/workflows/deploy-backend.yml; can also be run by hand.
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker
fi

# Firewall: only SSH + HTTP(S) (HTTP needed for Let's Encrypt HTTP-01 through Cloudflare)
if [ -f /etc/iptables/rules.v4 ] && grep -q REJECT /etc/iptables/rules.v4; then
  # Oracle Cloud images: iptables REJECT-all except SSH
  for port in 80 443; do
    iptables -C INPUT -p tcp --dport $port -j ACCEPT 2>/dev/null \
      || iptables -I INPUT 5 -p tcp -m state --state NEW --dport $port -j ACCEPT
  done
  command -v netfilter-persistent >/dev/null && netfilter-persistent save >/dev/null
elif command -v ufw >/dev/null; then
  ufw allow OpenSSH >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
  ufw --force enable >/dev/null || true
fi

# .env: create once with a random DB password, never overwrite user edits
if [ ! -f .env ]; then
  cp .env.example .env
  PW=$(openssl rand -hex 24)
  sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$PW/; s#^DATABASE_URL=.*#DATABASE_URL=postgresql+asyncpg://noctalix:$PW@db:5432/nowcast#" .env
fi
# R2 credentials passed from CI secrets (optional)
for k in R2_ACCOUNT_ID R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY; do
  v="${!k:-}"
  [ -n "$v" ] && sed -i "s#^$k=.*#$k=$v#" .env
done

PROFILES="--profile edge"
grep -qE '^CLOUDFLARE_TUNNEL_TOKEN=.+' .env && PROFILES="--profile tunnel"

docker compose $PROFILES up -d --build --remove-orphans
docker image prune -f >/dev/null
docker compose ps
