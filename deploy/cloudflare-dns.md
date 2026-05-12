# Cloudflare DNS + TLS — hoaproxy.org

## DNS records

In the Cloudflare dashboard for `hoaproxy.org`, set:

| Type | Name | Content              | Proxy   | TTL   |
|------|------|----------------------|---------|-------|
| A    | `@`  | `<HETZNER_IPV4>`     | Proxied | Auto  |
| A    | `www`| `<HETZNER_IPV4>`     | Proxied | Auto  |
| AAAA | `@`  | `<HETZNER_IPV6>`     | Proxied | Auto  |
| AAAA | `www`| `<HETZNER_IPV6>`     | Proxied | Auto  |

Get the IPs from the Hetzner Cloud Console once the server is provisioned.

Make sure both records are **Proxied** (orange cloud). Caddy is locked
down with an IP allowlist to Cloudflare's edge IPs (see `Caddyfile`), so
"DNS only" (grey cloud) will return 403 from origin.

## SSL/TLS mode

Settings → SSL/TLS → Overview → set encryption mode to **Full (strict)**.

## Origin certificate

Settings → SSL/TLS → Origin Server → **Create Certificate**:
- Hostnames: `hoaproxy.org`, `*.hoaproxy.org`
- Private key type: **RSA (2048)**
- Validity: **15 years**

Save the cert + private key as two files on the Hetzner server:
- `/etc/hoaproxy/cf-origin.crt` (the certificate)
- `/etc/hoaproxy/cf-origin.key` (the private key)

Permissions:
```bash
sudo chmod 600 /etc/hoaproxy/cf-origin.key
sudo chown root:root /etc/hoaproxy/cf-origin.crt /etc/hoaproxy/cf-origin.key
```

## Other Cloudflare settings worth flipping

- **Speed → Optimization → Brotli**: on
- **Speed → Optimization → Early Hints**: on
- **Rules → Page Rules** (or Cache Rules in the new UI):
  - `hoaproxy.org/static/*` → Cache Level: Cache Everything, Edge TTL: 1 month
  - `hoaproxy.org/healthz` → Cache Level: Bypass
- **Security → Bots → Bot Fight Mode**: on (free tier)
- **Network → HTTP/3 (with QUIC)**: on
- **Network → IPv6 Compatibility**: on

## Backup DNS during cutover

To avoid downtime, you can pre-create the Hetzner records as DNS-only
sub-records first (e.g. `new.hoaproxy.org`), validate end-to-end, then
swap the apex A records to the Hetzner IP when ready. Cloudflare's
proxy mode means TTL is effectively zero — propagation is seconds, not
hours.
