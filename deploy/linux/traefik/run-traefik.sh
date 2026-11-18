#!/usr/bin/env bash
# Start the reference Traefik: TLS on :443 for the platform origin and for every
# generated service (entrypoint-level TLS), :80 redirecting to it, the Docker
# provider for service routes, a file provider for the platform route, and the
# loopback-only metrics entrypoint. Needs the docker group.
#
# Required environment (no defaults, so nothing host-specific ships in the repo):
#   FORGE_PLATFORM_HOST   host part of PLATFORM_PUBLIC_URL, e.g. forge.example.com
#   FORGE_CERT_DIR        directory holding tls.crt and tls.key (see make-selfsigned-cert.sh)
# Optional:
#   FORGE_TRAEFIK_CONF    where the rendered dynamic config goes   (default /etc/forge/traefik)
#   FORGE_API_ADDR        host:port Traefik uses for the API        (default host.docker.internal:8000)
#   TRAEFIK_CONTAINER_NAME, TRAEFIK_METRICS_PORT, TRAEFIK_IMAGE  (defaults match .env.example)
#   --recreate            remove an existing container first. Recreating drops its
#                         attachments to the per-service networks until the platform
#                         reattaches them; prefer editing the dynamic file (watched).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${FORGE_PLATFORM_HOST:?set FORGE_PLATFORM_HOST (host of PLATFORM_PUBLIC_URL)}"
: "${FORGE_CERT_DIR:?set FORGE_CERT_DIR (directory with tls.crt and tls.key)}"
conf="${FORGE_TRAEFIK_CONF:-/etc/forge/traefik}"
api_addr="${FORGE_API_ADDR:-host.docker.internal:8000}"
name="${TRAEFIK_CONTAINER_NAME:-forge-traefik}"
metrics_port="${TRAEFIK_METRICS_PORT:-8082}"
# v3.6.1 or newer: older v3 pins Docker API 1.24, which recent engines reject.
image="${TRAEFIK_IMAGE:-traefik:v3.6}"

[ -r "$FORGE_CERT_DIR/tls.crt" ] && [ -r "$FORGE_CERT_DIR/tls.key" ] || {
  echo "no readable tls.crt / tls.key in $FORGE_CERT_DIR" >&2; exit 1; }

if docker inspect "$name" >/dev/null 2>&1; then
  if [ "${1:-}" = "--recreate" ]; then
    docker rm -f "$name" >/dev/null
  else
    echo "container $name exists; use --recreate, or edit $conf/dynamic (it is watched)" >&2
    exit 1
  fi
fi

mkdir -p "$conf/dynamic"
sed -e "s|__PLATFORM_HOST__|$FORGE_PLATFORM_HOST|g" -e "s|__API_ADDR__|$api_addr|g" \
  "$here/forge-platform.yml.template" > "$conf/dynamic/forge-platform.yml"

# 80/443 are published on every interface: they are the platform's public face.
# The docker socket is mounted read-only, which does not make it harmless: anything
# that controls this container's configuration controls the Docker API it can read.
docker run -d --name "$name" --restart unless-stopped \
  -p 80:80 -p 443:443 \
  --add-host=host.docker.internal:host-gateway \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  -v "$conf/dynamic:/dynamic:ro" \
  -v "$FORGE_CERT_DIR:/certs:ro" \
  "$image" \
  --providers.docker=true \
  --providers.docker.exposedbydefault=false \
  --providers.file.directory=/dynamic \
  --providers.file.watch=true \
  --entrypoints.web.address=:80 \
  --entrypoints.web.http.redirections.entryPoint.to=websecure \
  --entrypoints.web.http.redirections.entryPoint.scheme=https \
  --entrypoints.websecure.address=:443 \
  --entrypoints.websecure.http.tls=true \
  --entrypoints.forgemetrics.address="127.0.0.1:${metrics_port}" \
  --metrics.prometheus=true \
  --metrics.prometheus.entryPoint=forgemetrics

echo "started $name; in backend/.env set TRAEFIK_ENTRYPOINT=websecure, SERVICE_URL_SCHEME=https, TRAEFIK_METRICS_PORT=$metrics_port"
