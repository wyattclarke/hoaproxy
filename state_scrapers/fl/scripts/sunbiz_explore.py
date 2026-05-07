#!/usr/bin/env python3
"""Explore the Sunbiz public SFTP layout."""
import paramiko, sys

t = paramiko.Transport(("sftp.floridados.gov", 22))
t.connect(username="Public", password="PubAccess1845!")
s = paramiko.SFTPClient.from_transport(t)
def walk(path, depth=0, max_depth=3):
    try:
        entries = s.listdir_attr(path)
    except Exception as e:
        print(f"{'  '*depth}{path} ERROR {e}")
        return
    for e in entries:
        size = e.st_size or 0
        kind = "d" if (e.st_mode or 0) & 0o040000 else "f"
        print(f"{'  '*depth}[{kind}] {path.rstrip('/')}/{e.filename}  ({size} bytes)")
        if kind == "d" and depth < max_depth:
            walk(f"{path.rstrip('/')}/{e.filename}", depth+1, max_depth)

start = sys.argv[1] if len(sys.argv) > 1 else "/"
walk(start, max_depth=int(sys.argv[2]) if len(sys.argv) > 2 else 3)
s.close(); t.close()
