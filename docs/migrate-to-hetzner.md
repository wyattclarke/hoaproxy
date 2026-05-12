# Migrating hoaproxy.org from Render → Hetzner CCX23

End-to-end runbook for cutting the production site over from Render
(Standard, 1 CPU) to a Hetzner Cloud CCX23 (4 vCPU AMD EPYC, 16 GB RAM,
160 GB NVMe) in Ashburn, fronted by Cloudflare with a Caddy origin
reverse proxy.

**Target shape**

```
Cloudflare (proxied) → Caddy (TLS, IP allowlist) → docker-compose → uvicorn (FastAPI + worker)
                                                       │
                                                       ├─ /var/lib/hoaproxy/data   ← SQLite (local NVMe)
                                                       └─ /var/lib/hoaproxy/hoa_docs ← PDFs (200 GB Cloud Volume)
```

Approx. cost: **CCX23 €27.50/mo + 200 GB Volume €8/mo + Cloudflare free
= ~$36/mo**. Volume can be resized up to 10 TB online with no downtime.

---

## Phase 0 — On the existing Render service (do BEFORE provisioning Hetzner)

Goal: get a fresh DB snapshot + full hoa_docs mirror into GCS so the
new host has something to pull from.

1. **Deploy the new admin endpoint** (already on master; trigger redeploy):
   ```bash
   set -a && source settings.env && set +a
   curl -s -X POST "https://api.render.com/v1/services/$RENDER_SERVICE_ID/deploys" \
     -H "Authorization: Bearer $RENDER_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{"clearCache":"do_not_clear"}'
   ```
   Wait for the deploy to go live (check
   `https://api.render.com/v1/services/$RENDER_SERVICE_ID/deploys?limit=2`).

2. **Snapshot hoa_docs to GCS** (~1 TB transfer; runs inside Render):
   ```bash
   set -a && source settings.env && set +a
   curl -s -X POST "https://hoaproxy.org/admin/snapshot-hoa-docs-to-gcs" \
     -H "Authorization: Bearer $JWT_SECRET" \
     -H "Content-Type: application/json" \
     -d '{"dry_run": true}'
   ```
   Verify dry-run counts look right, then run for real:
   ```bash
   curl -s -X POST "https://hoaproxy.org/admin/snapshot-hoa-docs-to-gcs" \
     -H "Authorization: Bearer $JWT_SECRET" \
     -H "Content-Type: application/json" \
     -d '{"dry_run": false, "skip_existing": true}' \
     --max-time 7200
   ```
   The endpoint is idempotent — re-run if the connection drops mid-way.

3. **Take a fresh full DB snapshot to GCS** — use `/admin/backup-full`, NOT
   `/admin/backup` (the latter only dumps the precious-tables SQL subset):
   ```bash
   curl -s -X POST "https://hoaproxy.org/admin/backup-full" \
     -H "Authorization: Bearer $JWT_SECRET"
   ```
   The response includes `stamp` and `log_path`. Detached worker runs in
   the background on Render — poll for completion either by tailing the
   log or by checking the GCS blob:
   ```bash
   curl -s "https://hoaproxy.org/admin/backup-full-log?stamp=<STAMP>" \
     -H "Authorization: Bearer $JWT_SECRET"
   gsutil ls -l "gs://hoaproxy-backups/db/hoa_index-<STAMP>.db"
   ```

---

## Phase 1 — Provision Hetzner Cloud

1. Go to <https://console.hetzner.cloud>.
2. New project → **hoaproxy-prod**.
3. **Servers → Add server**:
   - Location: **Ashburn, VA (US-East)**
   - Image: **Ubuntu 24.04**
   - Type: **CCX23** (dedicated CPU, 4 vCPU AMD EPYC, 16 GB RAM, 160 GB NVMe)
   - Networking: IPv4 + IPv6
   - SSH key: paste the public key you use elsewhere (Render → GitHub → laptop)
   - Name: `hoaproxy-1`
   - Click **Create & Buy now**.
4. **Volumes → Add volume**:
   - Size: **200 GB** (resize up later as needed — online, no downtime)
   - Location: must match the server (Ashburn)
   - Attach to: `hoaproxy-1`
   - Name: `hoa-docs-1`
   - **Do not let Hetzner format it**; the bootstrap script does it.

Capture the IPv4 + IPv6 from the dashboard — you'll need them in Phase 4.

---

## Phase 2 — Bootstrap the server

```bash
# SSH in as root (Hetzner accepts root for the first login)
ssh root@<HETZNER_IPV4>

# Pull and run the bootstrap script
curl -fsSL https://raw.githubusercontent.com/wyattclarke/hoaproxy/master/deploy/server-bootstrap.sh -o /root/bootstrap.sh
bash /root/bootstrap.sh

# Reconnect as the hoaproxy user
exit
ssh hoaproxy@<HETZNER_IPV4>
```

What the script does (see `deploy/server-bootstrap.sh` for the source):
- creates `hoaproxy` sudo user with the same SSH key
- hardens SSH (no root login, no passwords)
- enables ufw firewall (22/80/443 only)
- 4 GB swap
- unattended-upgrades
- Docker + compose plugin
- Caddy
- Google Cloud SDK (`gsutil`)
- formats + mounts the Cloud Volume at `/var/lib/hoaproxy/hoa_docs`
- creates `/etc/hoaproxy/` for secrets

---

## Phase 3 — Drop secrets + clone the repo

On the Hetzner host, as the `hoaproxy` user:

### 3a. `/etc/hoaproxy/hoaproxy.env`

Copy these values out of Render → Environment, paste into a local file
on your laptop, then `scp` it up. **Do not paste secrets into chat.**

The 19 keys that matter:
```
ASYNC_INGEST_ENABLED=1
DOCUMENSO_API_URL=
EMAIL_FROM=...
EMAIL_PROVIDER=...
HOA_DISABLE_QDRANT=1
HOA_DOCAI_ENDPOINT=...
HOA_DOCAI_LOCATION=us
HOA_QDRANT_LOCAL_PATH=
INGEST_WORKER_ENABLED=1
INGEST_WORKER_HEARTBEAT_SEC=...
INGEST_WORKER_MAX_ATTEMPTS=...
INGEST_WORKER_POLL_SEC=...
JWT_SECRET=...
OPENAI_API_KEY=...
PROXY_RETENTION_DAYS=90
QA_API_KEY=...
QA_MODEL=...
SMTP_HOST=...
SMTP_PORT=...
SMTP_USER=...
```

Also set:
```
HOA_BANK_GCS_BUCKET=hoaproxy-bank
HOA_BACKUP_GCS_BUCKET=hoaproxy-backups
GOOGLE_APPLICATION_CREDENTIALS=/app/gcp-sa.json
HOA_DOCAI_PROJECT_ID=...    # if you re-enable DocAI later
HOA_DOCAI_PROCESSOR_ID=...
HOA_ENABLE_DOCAI=0          # start with DocAI off; flip to 1 after verifying
DAILY_DOCAI_BUDGET_USD=20
```

Then:
```bash
scp ./hoaproxy.env hoaproxy@<HETZNER_IPV4>:/tmp/
ssh hoaproxy@<HETZNER_IPV4>
sudo mv /tmp/hoaproxy.env /etc/hoaproxy/hoaproxy.env
sudo chown root:hoaproxy /etc/hoaproxy/hoaproxy.env
sudo chmod 640 /etc/hoaproxy/hoaproxy.env
```

### 3b. `/etc/hoaproxy/gcp-sa.json`

This is the existing `hoaware-ocr` service account key (already used for
DocAI + GCS backups). Get it out of Google Cloud → IAM → Service Accounts
→ hoaware-ocr → Keys → Add Key → JSON (or reuse the one you already have).

```bash
scp ./gcp-sa.json hoaproxy@<HETZNER_IPV4>:/tmp/
ssh hoaproxy@<HETZNER_IPV4>
sudo mv /tmp/gcp-sa.json /etc/hoaproxy/gcp-sa.json
sudo chown root:hoaproxy /etc/hoaproxy/gcp-sa.json
sudo chmod 640 /etc/hoaproxy/gcp-sa.json
```

### 3c. `/etc/hoaproxy/cf-origin.crt` + `cf-origin.key`

Follow `deploy/cloudflare-dns.md` to mint a 15-year Cloudflare Origin
cert, save crt + key to the same paths above.

### 3d. Clone the repo

```bash
cd ~
git clone https://github.com/wyattclarke/hoaproxy.git
cd hoaproxy
git checkout master
```

---

## Phase 4 — Restore data from GCS

```bash
cd ~/hoaproxy
bash deploy/restore-from-gcs.sh
```

This:
1. downloads the latest DB snapshot from `gs://hoaproxy-backups/db/`
2. rsyncs `gs://hoaproxy-backups/hoa_docs/` into `/var/lib/hoaproxy/hoa_docs/`
3. runs `PRAGMA quick_check` + prints HOA / document / file counts

Expect this to take **30–120 minutes** depending on hoa_docs size.

---

## Phase 5 — Bring the app up

```bash
cd ~/hoaproxy/deploy
docker compose -f docker-compose.prod.yml build app
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml logs -f app
```

Verify in another shell:
```bash
curl -s http://127.0.0.1:8000/healthz | jq .
curl -s http://127.0.0.1:8000/hoas/state-counts | jq '. | length'
```

Both should return 200 / sensible data.

---

## Phase 6 — Install Caddy

```bash
sudo cp ~/hoaproxy/deploy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl reload caddy
sudo systemctl status caddy
```

From your laptop, with `/etc/hosts` temporarily pointing `hoaproxy.org`
at the Hetzner IP, hit it directly:

```bash
echo "<HETZNER_IPV4> hoaproxy.org" | sudo tee -a /etc/hosts
curl -s -k https://hoaproxy.org/healthz | jq .
# remove the hosts entry afterwards
```

(The IP allowlist will 403 you because you're not Cloudflare — that's
expected. Test by spoofing `X-Forwarded-For` with a Cloudflare IP, or
just skip this step and cut over directly to Cloudflare in Phase 7.)

---

## Phase 7 — Cloudflare cutover

1. Add DNS records per `deploy/cloudflare-dns.md` (A + AAAA, both proxied).
2. Verify:
   ```bash
   curl -s https://hoaproxy.org/healthz
   curl -s https://hoaproxy.org/hoas/state-counts | jq 'length'
   ```
3. Spot-check from a browser: log in, run a search, view a doc, open an HOA page.

---

## Phase 8 — Install systemd + cron, then turn down Render

```bash
sudo cp ~/hoaproxy/deploy/hoaproxy.service /etc/systemd/system/hoaproxy.service
sudo cp ~/hoaproxy/deploy/hoaproxy-backup.cron /etc/cron.d/hoaproxy-backup
sudo systemctl daemon-reload
sudo systemctl enable hoaproxy
sudo mkdir -p /var/log/hoaproxy
sudo chown hoaproxy:hoaproxy /var/log/hoaproxy
```

Reboot test (optional but recommended):
```bash
sudo reboot
# wait 60s
ssh hoaproxy@<HETZNER_IPV4>
docker ps          # app container should be running
curl -s http://127.0.0.1:8000/healthz
```

**Render turn-down checklist**:
- [ ] Cloudflare DNS pointing at Hetzner for >48h with no error spikes
- [ ] At least one successful Hetzner backup in `gs://hoaproxy-backups/db/`
- [ ] cron-job.org `/admin/backup` URL still points to hoaproxy.org (now Hetzner) — no change needed
- [ ] Decommission Render service: Settings → Suspend (free) for one week, then Delete

---

## Future ops

| What | How |
|------|-----|
| Deploy a change | push to master, then `ssh hoaproxy@<host> 'cd hoaproxy && bash deploy/deploy.sh'` |
| Restart app | `cd hoaproxy/deploy && docker compose -f docker-compose.prod.yml restart app` |
| Tail logs | `docker compose -f docker-compose.prod.yml logs -f app` |
| Check DB integrity | `sqlite3 /var/lib/hoaproxy/data/hoa_index.db 'PRAGMA quick_check;'` |
| Manual backup | `bash ~/hoaproxy/deploy/backup.sh` |
| Restore from GCS | `bash ~/hoaproxy/deploy/restore-from-gcs.sh` (DESTRUCTIVE — moves current DB aside) |
| Grow the volume | Hetzner Console → Volumes → Resize, then `sudo resize2fs /dev/disk/by-id/scsi-0HC_Volume_<id>` |
| Reach the container shell | `docker exec -it hoaproxy-app bash` |

---

## Cost ceiling

- CCX23: €27.50 / mo (~$30)
- 200 GB Volume: €8 / mo (~$9). Resize to 1 TB later → €40/mo.
- Cloudflare: $0 (free tier covers everything we need)
- GCS (backups): ~$2/mo for 1.5 TB
- **Total at 1 TB hoa_docs: ~$80/mo** vs Render Standard $25 + bursting CPU
  + 65 GB persistent disk $13 = $38/mo with 1 CPU ceiling.
- Headroom: 4 dedicated vCPU @ 100% = no more 4-second responses.
