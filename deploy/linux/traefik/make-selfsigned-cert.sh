#!/usr/bin/env bash
# Self-signed certificate for the platform host and the wildcard of the apps base
# domain, for a test install (the WSL2 stand-in). A real install uses the
# organisation's CA or ACME instead: the CA choice is the operator's.
#
#   make-selfsigned-cert.sh <platform-host> <apps-base-domain> <out-dir>
#   make-selfsigned-cert.sh forge.example.com apps.forge.example.com /etc/forge/certs
set -euo pipefail
[ $# -eq 3 ] || { sed -n '2,8p' "${BASH_SOURCE[0]}"; exit 2; }
host="$1" apps="$2" out="$3"
mkdir -p "$out"
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -subj "/CN=$host" \
  -addext "subjectAltName=DNS:$host,DNS:*.$apps" \
  -keyout "$out/tls.key" -out "$out/tls.crt"
chmod 600 "$out/tls.key"
echo "wrote $out/tls.crt and $out/tls.key (valid 365 days)"
