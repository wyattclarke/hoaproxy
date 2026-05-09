#!/usr/bin/env python3
"""Trigger /admin/backup. Invoked by the hoaproxy-db-backup Render cron."""
import os
import sys
import urllib.error
import urllib.request

jwt = os.environ.get("JWT_SECRET")
if not jwt:
    print("JWT_SECRET not set", file=sys.stderr)
    sys.exit(2)

url = os.environ.get("BACKUP_URL", "https://hoaproxy.org/admin/backup")
req = urllib.request.Request(
    url,
    method="POST",
    headers={
        "Authorization": f"Bearer {jwt}",
        "User-Agent": "render-cron-backup",
        "Content-Length": "0",
    },
)
try:
    with urllib.request.urlopen(req, timeout=600) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        print(f"HTTP {resp.status}: {body[:500]}")
        sys.exit(0 if 200 <= resp.status < 300 else 1)
except urllib.error.HTTPError as e:
    body = e.read().decode("utf-8", errors="replace")
    print(f"HTTP {e.code}: {body[:500]}", file=sys.stderr)
    sys.exit(1)
except Exception as e:
    print(f"error: {e}", file=sys.stderr)
    sys.exit(1)
