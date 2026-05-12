#!/bin/bash
# Rolling deploy: pull latest master, rebuild image, restart container.
# Run as `hoaproxy` user from /home/hoaproxy/hoaproxy.
#
# Usage:  bash deploy/deploy.sh
#
# Downtime: ~30s while docker compose recreates the container. The container
# has a healthcheck so Caddy will return 502 briefly during the restart;
# Cloudflare returns its own 5xx page if it can't reach origin.

set -euo pipefail

cd "$(dirname "$0")/.."
REPO_DIR=$(pwd)

echo "[deploy] git pull"
git fetch --quiet origin master
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/master)
if [ "$LOCAL" = "$REMOTE" ]; then
    echo "[deploy] already at $LOCAL — nothing to do"
    exit 0
fi
git reset --hard origin/master

echo "[deploy] rebuilding container"
cd "$REPO_DIR/deploy"
docker compose -f docker-compose.prod.yml build app

echo "[deploy] recreating container"
docker compose -f docker-compose.prod.yml up -d --no-deps app

echo "[deploy] waiting for healthcheck"
for i in $(seq 1 30); do
    if docker inspect --format='{{.State.Health.Status}}' hoaproxy-app 2>/dev/null | grep -q "^healthy$"; then
        echo "[deploy] healthy after ${i}s × 2"
        echo "[deploy] DONE — now at $(git rev-parse --short HEAD)"
        exit 0
    fi
    sleep 2
done

echo "[deploy] WARNING: container not healthy after 60s; check 'docker compose logs app'" >&2
exit 1
