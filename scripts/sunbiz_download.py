#!/usr/bin/env python3
"""Download Sunbiz quarterly Non-Profit corporation data over public SFTP."""
import paramiko, sys, os, time

DEST_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "sunbiz")
DEST_DIR = os.path.abspath(DEST_DIR)
os.makedirs(DEST_DIR, exist_ok=True)

REMOTE = "/Public/doc/Quarterly/Non-Profit/npcordata.zip"
LOCAL = os.path.join(DEST_DIR, "npcordata.zip")

t = paramiko.Transport(("sftp.floridados.gov", 22))
t.connect(username="Public", password="PubAccess1845!")
s = paramiko.SFTPClient.from_transport(t)

attr = s.stat(REMOTE)
total = attr.st_size or 0
print(f"remote {REMOTE} size={total:,} bytes", flush=True)

start = time.time()
done = 0
last_t = start
last_d = 0
CHUNK = 32 * 1024

# Disable prefetch — server returns "insufficient resources" with default prefetching
with s.open(REMOTE, "rb") as rf:
    rf.set_pipelined(False)
    with open(LOCAL, "wb") as lf:
        while True:
            try:
                buf = rf.read(CHUNK)
            except OSError as e:
                # Throttle: brief sleep then retry once
                print(f"  retry on {e}", flush=True)
                time.sleep(2.0)
                buf = rf.read(CHUNK)
            if not buf:
                break
            lf.write(buf)
            done += len(buf)
            now = time.time()
            if now - last_t >= 5 or done == total:
                rate = (done - last_d) / max(now - last_t, 1e-9) / 1024 / 1024
                pct = 100.0 * done / total if total else 0
                print(f"  {done:,}/{total:,} ({pct:.1f}%)  {rate:.2f} MB/s", flush=True)
                last_t, last_d = now, done

elapsed = time.time() - start
print(f"done in {elapsed:.1f}s -> {LOCAL}")
s.close(); t.close()
