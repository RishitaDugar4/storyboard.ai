#!/usr/bin/env bash
# Prepare a fresh Ubuntu 24.04 box. Run ONCE, on the server, as root or sudo.
#
#   scp deploy/bootstrap.sh root@YOUR_HOST:/tmp/
#   ssh root@YOUR_HOST 'bash /tmp/bootstrap.sh'
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/hbday-zee}
SWAP_GB=${SWAP_GB:-4}

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

log "Updating base system"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get upgrade -y -qq
apt-get install -y -qq ca-certificates curl gnupg rsync ufw unattended-upgrades

log "Installing Docker Engine + Compose plugin"
if ! command -v docker >/dev/null; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io \
      docker-buildx-plugin docker-compose-plugin
fi
docker --version && docker compose version

log "Firewall: SSH, HTTP, HTTPS only"
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp   >/dev/null
ufw allow 443/tcp  >/dev/null
ufw allow 443/udp  >/dev/null      # HTTP/3
ufw --force enable
ufw status verbose | head -12

# An 8GB box running ffmpeg alongside Postgres benefits from swap as a safety
# net. It should never be the working set -- it is there to avoid the OOM
# killer taking out Postgres mid-render.
if [ ! -f /swapfile ] && [ "$SWAP_GB" -gt 0 ]; then
  log "Adding ${SWAP_GB}G swap"
  fallocate -l "${SWAP_GB}G" /swapfile
  chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  sysctl -w vm.swappiness=10 >/dev/null
  echo 'vm.swappiness=10' > /etc/sysctl.d/99-swappiness.conf
fi

log "Enabling unattended security upgrades"
dpkg-reconfigure -f noninteractive unattended-upgrades

log "Creating $APP_DIR"
mkdir -p "$APP_DIR/backups"

cat <<NOTE

  Bootstrap complete.

  Next, from your laptop:
    1. scp .env.prod.example  root@THIS_HOST:$APP_DIR/.env.prod
       ssh root@THIS_HOST 'nano $APP_DIR/.env.prod'      # fill in every value
    2. make deploy DEPLOY_HOST=root@THIS_HOST

  Confirm the DNS A record for SITE_ADDRESS points here BEFORE deploying, or
  Caddy will fail to obtain a certificate and back off.

NOTE
