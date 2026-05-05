#!/usr/bin/env python3
"""Parse Sunbiz Non-Profit quarterly fixed-width records and emit HOA seed JSONL.

Records are 1440 bytes + CRLF.  Field offsets follow the cor.html definitions:
- 0..12   (12)  document/corp number
- 12..204 (192) corporation name
- 204..205 (1)  status (A/I)
- 205..220 (15) filing type (DOMNP, NPREG, etc.)
- 220..262 (42) principal addr street 1
- 262..304 (42) principal addr street 2
- 304..332 (28) principal addr city
- 332..334 (2)  principal addr state
- 334..344 (10) principal addr zip (zip5+zip4 + filler)
- 344..346 (2)  principal addr country
- 346..388 (42) mailing addr street 1
- 388..430 (42) mailing addr street 2
- 430..458 (28) mailing addr city
- 458..460 (2)  mailing addr state
- 460..470 (10) mailing addr zip
- 470..472 (2)  mailing addr country
- 472..480 (8)  file date YYYYMMDD
- 480..482 (2)  state of incorporation
- 482..483 (1)  more
- 483..485 (2)  state of incorporation country?
- 485..495 (10) more  (we don't care)
- 495..503 (8)  last transaction date
- 503..504 (1)  more
- 504..544 (40) (events/etc.)
- 544..586 (42) registered agent name
- 586..628 (42) RA street1
- 628..670 (42) RA street2
- 670..698 (28) RA city
- 698..700 (2)  RA state
- 700..710 (10) RA zip
- 710..712 (2)  RA country
- 712..717 (5)  RA type / "RAR"
- 717..1440      6 officer slots

We only emit a row if the name matches an HOA/condo/master/POA/townhome/villa/
property-owners pattern *and* status=A.  Output JSONL with name, doc_no, status,
filing_type, principal {street, city, state, zip}, mailing {...},
registered_agent {name, street, city, state, zip}.
"""
from __future__ import annotations
import argparse, glob, json, os, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# HOA/condo name pattern.  Inclusive of: HOA, POA, condo, townhome, master,
# property owners, homeowners, villas-of-X-association etc.  Avoid matching
# "Mason ASSOCIATION" (a single capitalized noun + "Association" alone is too
# generic) — require the noun to be community-shaped or association-typed.
NAME_RE = re.compile(
    r"\b("
    r"HOMEOWNERS?\s+ASSOC"
    r"|HOME\s+OWNERS?\s+ASSOC"
    r"|HOMES?\s+ASSOC"
    r"|PROPERTY\s+OWNERS?\s+ASSOC"
    r"|PROPERTY\s+OWNER\s+ASSOC"
    r"|CONDOMINIUM\s+ASSOC"
    r"|CONDOMINIUMS?\s+OF\s+"  # "Condominiums of Foo"
    r"|CONDO\s+ASSOC"
    r"|MASTER\s+ASSOC"
    r"|TOWNHOME\s+ASSOC"
    r"|TOWN\s*HOMES?\s+ASSOC"
    r"|TOWNHOMES?\s+ASSOC"
    r"|VILLAS?\s+ASSOC"
    r"|VILLAGE\s+ASSOC"
    r"|NEIGHBORHOOD\s+ASSOC"
    r"|COMMUNITY\s+ASSOC"
    r"|MAINTENANCE\s+ASSOC"
    r"|RESIDENTS?\s+ASSOC"
    r"|HOMESITE\s+ASSOC"
    r"|HOMESITE\s+OWNERS?"
    r"|UNIT\s+OWNERS?\s+ASSOC"
    r")",
    re.IGNORECASE,
)
# Hard rejects: voluntary civic / professional / religious / business associations.
REJECT_NAME_RE = re.compile(
    r"\b("
    r"BAR\s+ASSOC|BUSINESS\s+ASSOC|MERCHANTS?\s+ASSOC|CHAMBER\s+OF"
    r"|REALTORS?\s+ASSOC|REALTY\s+ASSOC|LANDLORDS?\s+ASSOC"
    r"|INVESTMENT\s+ASSOC|INVESTORS?\s+ASSOC"
    r"|PARENT\s+TEACHER|PTA\s|PTO\s|ALUMNI"
    r"|CHURCH|MINISTRY|TEMPLE|SYNAGOGUE|MOSQUE"
    r"|VETERAN|VFW\s|AMERICAN\s+LEGION"
    r"|BOOSTER|ATHLETIC|SOCCER|BASEBALL|FOOTBALL|TENNIS\s+CLUB"
    r"|GARDEN\s+CLUB|YACHT\s+CLUB|GOLF\s+CLUB|BRIDGE\s+CLUB"
    r"|FRATERN|SORORITY|MASONIC|LODGE\s|ROTARY|KIWANIS|LIONS\s+CLUB"
    r"|CONFERENCE|FOUNDATION\s|CHARITY"
    r"|MEDICAL\s+ASSOC|DENTAL\s+ASSOC|HOSPITAL"
    r")",
    re.IGNORECASE,
)


def slice_str(rec: bytes, start: int, length: int) -> str:
    return rec[start:start + length].decode("latin-1", errors="replace").rstrip().strip()


def parse_record(rec: bytes) -> dict | None:
    if len(rec) < 1440:
        return None
    name = slice_str(rec, 12, 192)
    status = slice_str(rec, 204, 1)
    filing_type = slice_str(rec, 205, 15)
    if status != "A":
        return None
    if not NAME_RE.search(name):
        return None
    if REJECT_NAME_RE.search(name):
        return None
    return {
        "doc_no": slice_str(rec, 0, 12),
        "name": name,
        "status": status,
        "filing_type": filing_type,
        "principal": {
            "street1": slice_str(rec, 220, 42),
            "street2": slice_str(rec, 262, 42),
            "city": slice_str(rec, 304, 28),
            "state": slice_str(rec, 332, 2),
            "zip": slice_str(rec, 334, 10),
            "country": slice_str(rec, 344, 2),
        },
        "mailing": {
            "street1": slice_str(rec, 346, 42),
            "street2": slice_str(rec, 388, 42),
            "city": slice_str(rec, 430, 28),
            "state": slice_str(rec, 458, 2),
            "zip": slice_str(rec, 460, 10),
            "country": slice_str(rec, 470, 2),
        },
        "file_date": slice_str(rec, 472, 8),
        "registered_agent": {
            "name": slice_str(rec, 544, 42),
            "street1": slice_str(rec, 586, 42),
            "city": slice_str(rec, 670, 28),
            "state": slice_str(rec, 698, 2),
            "zip": slice_str(rec, 700, 10),
        },
    }


def iter_records(path: Path):
    """Yield 1440-byte records, tolerant of CRLF row terminators."""
    with path.open("rb") as f:
        data = f.read()
    # Split on CRLF; some rows may include embedded special chars but the file
    # is fixed-width so most rows are exactly 1440 bytes followed by \r\n.
    i = 0
    n = len(data)
    while i < n:
        end = i + 1440
        if end > n:
            break
        rec = data[i:end]
        # Skip CRLF terminator if present
        i = end
        if i < n and data[i:i+2] == b"\r\n":
            i += 2
        elif i < n and data[i:i+1] in (b"\n", b"\r"):
            i += 1
        yield rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-dir", default=str(ROOT / "data" / "sunbiz"),
                    help="dir containing npcordata*.txt")
    ap.add_argument("--output", default=str(ROOT / "data" / "fl_sunbiz_hoas.jsonl"))
    ap.add_argument("--rejected-sample", default=str(ROOT / "data" / "fl_sunbiz_rejected_sample.jsonl"),
                    help="write a sample of rejected (active) names for QA")
    ap.add_argument("--rejected-sample-n", type=int, default=200)
    args = ap.parse_args()

    paths = sorted(glob.glob(os.path.join(args.input_dir, "npcordata*.txt")))
    if not paths:
        print(f"no npcordata*.txt in {args.input_dir}", file=sys.stderr)
        return 1

    total = 0
    kept = 0
    rejected_sample = []
    out = open(args.output, "w")
    for path in paths:
        p = Path(path)
        file_total = 0
        file_kept = 0
        for rec in iter_records(p):
            total += 1
            file_total += 1
            row = parse_record(rec)
            if row is None:
                # Maybe collect a rejected sample for QA
                if len(rejected_sample) < args.rejected_sample_n and (total % 5000 == 0):
                    name = slice_str(rec, 12, 192)
                    status = slice_str(rec, 204, 1)
                    if status == "A" and name:
                        rejected_sample.append({"name": name})
                continue
            out.write(json.dumps(row, sort_keys=True) + "\n")
            kept += 1
            file_kept += 1
        print(f"{p.name}: total={file_total:,} kept={file_kept:,}", file=sys.stderr)
    out.close()

    if rejected_sample:
        with open(args.rejected_sample, "w") as f:
            for r in rejected_sample:
                f.write(json.dumps(r) + "\n")

    print(f"\nTOTAL records read: {total:,}", file=sys.stderr)
    print(f"TOTAL HOAs kept   : {kept:,}", file=sys.stderr)
    print(f"output: {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
