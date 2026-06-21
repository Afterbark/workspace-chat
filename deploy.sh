#!/usr/bin/env bash
# Pull the latest code and (re)build + restart the stack on the droplet.
set -euo pipefail
cd "$(dirname "$0")"

git pull --ff-only
docker compose up -d --build
docker compose ps
echo "Deployed. Tail logs with:  docker compose logs -f web"
