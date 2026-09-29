#!/usr/bin/env bash
# Deploy or update the Wage Type Catalog Service on this host.
#
#   First time:  SITE_ADDRESS=13-62-135-9.sslip.io ./deploy/deploy.sh
#   Updates:     ./deploy/deploy.sh
#
# Pulls the latest main, rebuilds the API image and restarts only this project's containers.
# Other Compose projects on the host (e.g. manon) are never touched.
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -f .env ]]; then
  : "${SITE_ADDRESS:?first deploy: run as SITE_ADDRESS=<hostname> ./deploy/deploy.sh}"
  umask 077
  cat > .env <<EOF
# Created by deploy.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ). Never commit this file.
SITE_ADDRESS=${SITE_ADDRESS}
WTC_JWT_SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')
WTC_ENABLE_DOCS=true
WTC_LOG_FORMAT=json
EOF
  echo "Created deploy/.env for ${SITE_ADDRESS}"
fi

echo "==> Pulling latest code"
git -C .. pull --ff-only

echo "==> Building API image"
docker compose build api

echo "==> Starting containers"
docker compose up -d --remove-orphans

echo "==> Waiting for the API to become healthy"
for _ in $(seq 1 30); do
  status=$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q api)" 2>/dev/null || echo starting)
  [[ "$status" == "healthy" ]] && break
  sleep 2
done
docker compose ps
[[ "$status" == "healthy" ]] || { echo "API did not become healthy"; docker compose logs --tail 50 api; exit 1; }

docker image prune -f >/dev/null
echo "==> Deployed: https://$(grep '^SITE_ADDRESS=' .env | cut -d= -f2)/docs"
