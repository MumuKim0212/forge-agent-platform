#!/usr/bin/env bash
# Build (and migrate) the platform on a Linux host. Run as the service user from
# anywhere; it works from the repo root it lives in. Safe to re-run.
#
#   deploy/linux/build.sh                 venv + frontend + images + migrate
#   deploy/linux/build.sh --skip-migrate  everything but `alembic upgrade head`
#   deploy/linux/build.sh --migrate-only  only `alembic upgrade head`
#   --skip-frontend / --skip-images       leave that step out
#   --with-openai-image                   also build the OpenAI engine's sandbox image
#
# It never starts or stops a unit: stop forge-worker and forge-api before an
# upgrade that migrates (README.md §Upgrade).
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
backend="$repo_root/backend"
frontend="$repo_root/frontend"

do_venv=1 do_frontend=1 do_images=1 do_migrate=1 openai_image=0
for arg in "$@"; do
  case "$arg" in
    --skip-migrate) do_migrate=0 ;;
    --skip-frontend) do_frontend=0 ;;
    --skip-images) do_images=0 ;;
    --with-openai-image) openai_image=1 ;;
    --migrate-only) do_venv=0 do_frontend=0 do_images=0 do_migrate=1 ;;
    -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing prerequisite: $1 ($2)" >&2; exit 1; }; }
need git "the platform shells out to git"
[ "$do_venv" = 1 ] && need python3 "apt-get install python3 python3-venv"
[ "$do_frontend" = 1 ] && need npm "apt-get install nodejs npm (Node 22, as CI uses)"
[ "$do_images" = 1 ] && need docker "README.md §Docker Engine; this user must be in the docker group"

cd "$backend"

if [ "$do_venv" = 1 ]; then
  echo "[build] python venv from constraints.txt"
  [ -x .venv/bin/python ] || python3 -m venv .venv
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -c constraints.txt -e .
fi

if [ "$do_frontend" = 1 ]; then
  echo "[build] frontend dist"
  cd "$frontend"
  npm ci
  npx vite build
  cd "$backend"
fi

if [ "$do_images" = 1 ]; then
  echo "[build] images (from backend/, as the Dockerfiles require)"
  docker build -t forge-llm-proxy:latest -f docker/llm-proxy/Dockerfile .
  docker build -t forge-agent-sandbox:latest -f docker/agent-sandbox/Dockerfile .
  if [ "$openai_image" = 1 ]; then
    docker build -t forge-agent-sandbox-openai:latest -f docker/agent-sandbox-openai/Dockerfile .
  fi
  # The platform creates each project's Redis without pulling, so a fresh host needs the image
  # before the first deployment. Honour SERVICE_REDIS_IMAGE from the environment or backend/.env.
  redis_image="${SERVICE_REDIS_IMAGE:-}"
  if [ -z "$redis_image" ] && [ -f .env ]; then
    redis_image="$(sed -n 's/^SERVICE_REDIS_IMAGE=//p' .env | tail -n 1)"
  fi
  redis_image="${redis_image:-redis:8-alpine}"
  echo "[build] pulling the service Redis image $redis_image"
  docker pull "$redis_image"
fi

if [ "$do_migrate" = 1 ]; then
  [ -f .env ] || { echo "backend/.env is missing; create it first (README.md §.env)" >&2; exit 1; }
  echo "[build] alembic upgrade head"
  .venv/bin/python -m alembic upgrade head
fi

echo "[build] done"
