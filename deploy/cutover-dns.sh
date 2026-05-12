#!/bin/bash
# DNS cutover: point hoaproxy.org + www at the Hetzner server.
#
# Replaces the existing CNAMEs (→ hoaware-app.onrender.com, proxied=False) with
# A records → 5.78.221.146, proxied=True. Cloudflare's proxy is REQUIRED because
# the Hetzner Caddy origin is IP-allowlisted to Cloudflare edge IPs only.
#
# Reads CLOUDFLARE_API_KEY from settings.env. Idempotent — won't double-write.
#
# Usage:
#   bash deploy/cutover-dns.sh         # apply the flip
#   bash deploy/cutover-dns.sh --rollback  # revert to Render CNAMEs

set -eo pipefail

cd "$(dirname "$0")/.."
set -a
# shellcheck disable=SC1091
source settings.env
set +a

ZONE_ID="11bb196ab5dc995d6c575ffcad2ee487"
HETZNER_IPV4="5.78.221.146"
RENDER_CNAME="hoaware-app.onrender.com"
APEX_RECORD_ID="413284e18c95faf172eddeb87aed1c28"
WWW_RECORD_ID="130a7d482d2e6cacbb962bce511504d0"

API="https://api.cloudflare.com/client/v4"
AUTH="Authorization: Bearer $CLOUDFLARE_API_KEY"

flip_to_hetzner() {
  local record_id=$1
  local name=$2
  local payload
  payload=$(cat <<EOF
{
  "type": "A",
  "name": "$name",
  "content": "$HETZNER_IPV4",
  "ttl": 1,
  "proxied": true,
  "comment": "Hetzner CCX23 Hillsboro (cutover 2026-05-12)"
}
EOF
)
  echo "PATCH $name -> A $HETZNER_IPV4 (proxied)"
  curl -sw "  HTTP %{http_code}\n" -X PUT "$API/zones/$ZONE_ID/dns_records/$record_id" \
    -H "$AUTH" -H "Content-Type: application/json" \
    --data "$payload" | python3 -c "
import sys,json
raw = sys.stdin.read()
http = raw.rsplit('HTTP ', 1)[-1].strip()
body = raw[:raw.rfind('HTTP ')] if 'HTTP ' in raw else raw
try:
    d = json.loads(body)
    print('  success:', d.get('success'))
    if not d.get('success'):
        print('  errors:', d.get('errors'))
    else:
        r = d.get('result', {})
        print('  now:', r.get('type'), r.get('name'), '->', r.get('content'), 'proxied=', r.get('proxied'))
except Exception as e:
    print('  parse error:', e)
print('  http:', http)
"
}

flip_to_render() {
  local record_id=$1
  local name=$2
  local payload
  payload=$(cat <<EOF
{
  "type": "CNAME",
  "name": "$name",
  "content": "$RENDER_CNAME",
  "ttl": 1,
  "proxied": false,
  "comment": "Reverted to Render origin"
}
EOF
)
  echo "PUT $name -> CNAME $RENDER_CNAME (DNS-only)"
  curl -sw "  HTTP %{http_code}\n" -X PUT "$API/zones/$ZONE_ID/dns_records/$record_id" \
    -H "$AUTH" -H "Content-Type: application/json" \
    --data "$payload" | head -3
}

if [ "${1:-}" = "--rollback" ]; then
    echo "=== ROLLBACK: hoaproxy.org + www → Render CNAMEs ==="
    flip_to_render "$APEX_RECORD_ID" "hoaproxy.org"
    flip_to_render "$WWW_RECORD_ID" "www.hoaproxy.org"
else
    echo "=== CUTOVER: hoaproxy.org + www → Hetzner $HETZNER_IPV4 (proxied) ==="
    flip_to_hetzner "$APEX_RECORD_ID" "hoaproxy.org"
    flip_to_hetzner "$WWW_RECORD_ID" "www.hoaproxy.org"
fi

echo
echo "DNS flip done. Cloudflare proxy = effectively zero TTL; the new origin"
echo "will be reachable from any new request within seconds."
