#!/bin/bash
# One-shot bootstrap for a fresh Ubuntu 24.04 Hetzner CCX23 server.
#
# Run as root immediately after first SSH login. Sets up:
#   - non-root sudo user `hoaproxy` with the same SSH key as root
#   - Docker + docker compose v2
#   - Caddy (reverse proxy + TLS)
#   - Google Cloud SDK (gsutil for backups)
#   - ufw firewall (ports 22, 80, 443 from anywhere; rest closed)
#   - unattended-upgrades for security patches
#   - swap file (4 GB — useful headroom for OCR/embedding RAM spikes)
#   - systemd unit + cloud volume mount for /var/lib/hoaproxy/hoa_docs
#
# Idempotent — safe to re-run. Logs to stderr; prints PASS/FAIL per step.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/wyattclarke/hoaproxy/master/deploy/server-bootstrap.sh | bash
# or:
#   scp deploy/server-bootstrap.sh root@<server-ip>:/root/
#   ssh root@<server-ip> bash /root/server-bootstrap.sh

set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

USER_NAME="hoaproxy"
DATA_DIR="/var/lib/hoaproxy"
ENV_DIR="/etc/hoaproxy"
VOLUME_DEVICE_HINT="/dev/disk/by-id/scsi-0HC_Volume_*"

step() { echo "=== [bootstrap] $* ==="; }

# 1. System packages
step "1. apt-get update + base packages"
apt-get update -y
apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg lsb-release ufw fail2ban htop tmux jq \
    rsync sqlite3 unattended-upgrades software-properties-common debian-keyring \
    debian-archive-keyring apt-transport-https

# 2. Non-root user
step "2. create sudo user '${USER_NAME}'"
if ! id -u "$USER_NAME" >/dev/null 2>&1; then
    useradd -m -s /bin/bash "$USER_NAME"
    usermod -aG sudo "$USER_NAME"
    # Inherit root's authorized_keys so the user can ssh in with the same key
    mkdir -p "/home/${USER_NAME}/.ssh"
    cp /root/.ssh/authorized_keys "/home/${USER_NAME}/.ssh/authorized_keys"
    chown -R "${USER_NAME}:${USER_NAME}" "/home/${USER_NAME}/.ssh"
    chmod 700 "/home/${USER_NAME}/.ssh"
    chmod 600 "/home/${USER_NAME}/.ssh/authorized_keys"
fi
# Passwordless sudo for the user — convenient for the runbook commands
echo "${USER_NAME} ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/${USER_NAME}
chmod 440 /etc/sudoers.d/${USER_NAME}

# 3. SSH hardening — disable root login + password auth
step "3. SSH hardening"
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -i 's/^#\?ChallengeResponseAuthentication.*/ChallengeResponseAuthentication no/' /etc/ssh/sshd_config
systemctl restart ssh

# 4. Firewall
step "4. ufw firewall"
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp comment "ssh"
ufw allow 80/tcp comment "http (cloudflare)"
ufw allow 443/tcp comment "https (cloudflare)"
ufw --force enable

# 5. Swap file
step "5. swap (4 GB)"
if [ ! -f /swapfile ]; then
    fallocate -l 4G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# 6. Unattended-upgrades
step "6. unattended-upgrades"
dpkg-reconfigure -plow unattended-upgrades || true

# 7. Docker
step "7. docker + compose plugin"
if ! command -v docker >/dev/null 2>&1; then
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list
    apt-get update -y
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
usermod -aG docker "$USER_NAME"
systemctl enable --now docker

# 8. Caddy
step "8. caddy"
if ! command -v caddy >/dev/null 2>&1; then
    curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -y
    apt-get install -y caddy
fi
systemctl enable caddy

# 9. Google Cloud SDK (gsutil)
step "9. google cloud sdk"
if ! command -v gsutil >/dev/null 2>&1; then
    curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg
    echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] http://packages.cloud.google.com/apt cloud-sdk main" > /etc/apt/sources.list.d/google-cloud-sdk.list
    apt-get update -y
    apt-get install -y google-cloud-cli
fi

# 10. Data directories
step "10. data directories"
mkdir -p "${DATA_DIR}/data" "${DATA_DIR}/hoa_docs" "${ENV_DIR}" /var/log/caddy
chown -R "${USER_NAME}:${USER_NAME}" "${DATA_DIR}"
chmod 700 "${ENV_DIR}"

# 11. Hetzner Cloud Volume for hoa_docs
step "11. mount Hetzner Cloud Volume for hoa_docs (if present)"
# Hetzner volumes appear as /dev/disk/by-id/scsi-0HC_Volume_<id>.
# If a volume is attached and not yet formatted, format it ext4 and mount
# at ${DATA_DIR}/hoa_docs. This is idempotent.
shopt -s nullglob
VOL_DEV=""
for cand in $VOLUME_DEVICE_HINT; do VOL_DEV="$cand"; break; done
if [ -n "$VOL_DEV" ] && [ -b "$VOL_DEV" ]; then
    if ! blkid "$VOL_DEV" >/dev/null 2>&1; then
        echo "  formatting $VOL_DEV as ext4"
        mkfs.ext4 -F "$VOL_DEV"
    fi
    UUID=$(blkid -s UUID -o value "$VOL_DEV")
    if ! grep -q "$UUID" /etc/fstab; then
        echo "UUID=$UUID ${DATA_DIR}/hoa_docs ext4 defaults,nofail,discard 0 0" >> /etc/fstab
    fi
    if ! mountpoint -q "${DATA_DIR}/hoa_docs"; then
        mount "${DATA_DIR}/hoa_docs"
    fi
    chown -R "${USER_NAME}:${USER_NAME}" "${DATA_DIR}/hoa_docs"
    echo "  volume mounted: ${DATA_DIR}/hoa_docs"
else
    echo "  no Hetzner Cloud Volume attached — hoa_docs will live on the local NVMe"
fi

step "DONE — server is bootstrapped."
echo
echo "Next steps (run as the '${USER_NAME}' user):"
echo "  1. Drop /etc/hoaproxy/hoaproxy.env  (env vars; see runbook)"
echo "  2. Drop /etc/hoaproxy/gcp-sa.json   (GCP service account key)"
echo "  3. Drop /etc/hoaproxy/cf-origin.crt + cf-origin.key (Cloudflare Origin cert)"
echo "  4. Clone the repo to /home/${USER_NAME}/hoaproxy"
echo "  5. cp deploy/Caddyfile /etc/caddy/Caddyfile && systemctl reload caddy"
echo "  6. Restore DB:    sudo -u ${USER_NAME} bash deploy/restore-from-gcs.sh"
echo "  7. Start the app: cd deploy && docker compose -f docker-compose.prod.yml up -d --build"
echo
echo "See docs/migrate-to-hetzner.md for the full runbook."
