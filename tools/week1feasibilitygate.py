#!/usr/bin/env python3
"""
Chicagoland hospital MRF feasibility gate.

Week‑1 go/no‑go harness for the capstone. Three stages:

    python chicago_mrf_gate.py discover  -> read cms-hpt.txt from each health system
    python chicago_mrf_gate.py probe     -> HEAD each MRF for size and content type
    python chicago_mrf_gate.py sample    -> download the smallest files, extract payers

Deps:  pip install requests ijson
"""



import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse

import requests

try:
    import ijson 
except ImportError:
    ijson = None


# Health systems confirmed to publish a cms-hpt.txt at their domain root.
SYSTEMS = {
    "Northwestern Medicine": "https://www.nm.org/cms-hpt.txt",
    "Advocate Health": "https://www.advocatehealth.com/cms-hpt.txt",
    "Endeavor Health": "https://www.endeavorhealth.org/cms-hpt.txt",
    "UChicago Medicine": "https://www.uchicagomedicine.org/cms-hpt.txt",
    "Loyola / Trinity": "https://www.loyolamedicine.org/cms-hpt.txt",
    "Rush": "https://www.rush.edu/cms-hpt.txt",
    
}

OUT = Path("gate_output")
HOSPITALS_CSV = OUT / "hospitals.csv"
PROBED_CSV = OUT / "hospitals_probed.csv"
DOWNLOADS = OUT / "downloads"
UA = {"User-Agent": "capstone-research/1.0 (academic use)"}
TIMEOUT = 30


def normalize(url: str) -> str:
    """cms-hpt.txt files routinely omit the scheme. CMS says they shouldn't."""
    url = (url or "").strip()
    if not url:
        return ""
    if not urlparse(url).scheme:
        url = "https://" + url.lstrip("/")
    return url


def parse_hpt_txt(text: str) -> list[dict]:
    """
    cms-hpt.txt is blank-line-separated blocks of `key: value` pairs.
    Real files are messy: stray blank lines, inconsistent casing, repeated
    contacts. Parse defensively rather than assuming the spec is honored.
    """
    records, current = [], {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            if current:
                records.append(current)
                current = {}
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        current[key.strip().lower()] = value.strip()
    if current:
        records.append(current)
    return [r for r in records if r.get("mrf-url")]


def guess_format(url: str) -> str:
    low = url.lower()
    for ext in ("json", "csv", "xlsx", "xml"):
        if ext in low:
            return ext
    return "unknown"


def discover() -> None:
    OUT.mkdir(exist_ok=True)
    rows = []
    for system, txt_url in SYSTEMS.items():
        try:
            resp = requests.get(txt_url, headers=UA, timeout=TIMEOUT)
            resp.raise_for_status()
        except Exception as exc:
            print(f"  [MISS] {system:24s} {type(exc).__name__}: {exc}")
            continue

        found = parse_hpt_txt(resp.text)
        print(f"  [ OK ] {system:24s} {len(found)} location(s)")
        for rec in found:
            mrf = normalize(rec.get("mrf-url", ""))
            rows.append(
                {
                    "system": system,
                    "location": rec.get("location-name", "").strip(),
                    "mrf_url": mrf,
                    "source_url": normalize(rec.get("source-page-url", "")),
                    "format": guess_format(mrf),
                    "direct_download": "yes" if guess_format(mrf) != "unknown" else "no",
                }
            )

    with HOSPITALS_CSV.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    fmts = Counter(r["format"] for r in rows)
    print(f"\n{len(rows)} hospitals -> {HOSPITALS_CSV}")
    print(f"formats: {dict(fmts)}")
    portals = [r for r in rows if r["direct_download"] == "no"]
    if portals:
        print(f"\n{len(portals)} entries point at a portal rather than a file:")
        for r in portals:
            print(f"  - {r['system']}: {r['location']}")
        print("  Treat these as non-compliant for your purposes and skip them.")


def probe() -> None:
    """HEAD every MRF. Size is what decides which files you can actually work with."""
    rows = list(csv.DictReader(HOSPITALS_CSV.open(encoding="utf-8")))
    for row in rows:
        url = row["mrf_url"]
        try:
            resp = requests.head(url, headers=UA, timeout=TIMEOUT, allow_redirects=True)
            size = int(resp.headers.get("content-length", 0))
            row["status"] = str(resp.status_code)
            row["size_mb"] = f"{size / 1_048_576:.1f}" if size else "unknown"
            row["content_type"] = resp.headers.get("content-type", "").split(";")[0]
        except Exception as exc:
            row["status"] = f"ERR {type(exc).__name__}"
            row["size_mb"] = row["content_type"] = ""
        print(f"  {row['size_mb']:>10} MB  {row['status']:>12}  {row['location'][:50]}")

    with PROBED_CSV.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    sizes = [float(r["size_mb"]) for r in rows if r.get("size_mb", "").replace(".", "").isdigit()]
    if sizes:
        print(f"\nmedian {sorted(sizes)[len(sizes) // 2]:.0f} MB, max {max(sizes):.0f} MB")
    print(f"-> {PROBED_CSV}")


def payers_from_json(path: Path) -> Counter:
    """Stream payer names out of a CMS-schema standardcharges.json."""
    payers = Counter()
    if ijson is None:
        print("  ijson not installed; skipping JSON streaming")
        return payers
    with path.open("rb") as fh:
        try:
            for name in ijson.items(fh, "standard_charge_information.item"):
                for group in name.get("standard_charges", []):
                    for payer in group.get("payers_information", []):
                        label = f"{payer.get('payer_name','?')} | {payer.get('plan_name','?')}"
                        payers[label] += 1
        except Exception as exc:
            print(f"  parse error: {exc}")
    return payers


def payers_from_csv(path: Path) -> Counter:
    """CMS 'tall' CSV encodes payer and plan as their own columns."""
    payers = Counter()
    with path.open(encoding="utf-8", errors="replace") as fh:
        # CMS CSVs carry two header rows of hospital metadata before the real header.
        lines = fh.readlines()
    header_idx = next(
        (i for i, l in enumerate(lines[:10]) if "payer_name" in l.lower()), 0
    )
    reader = csv.DictReader(lines[header_idx:])
    for row in reader:
        payer = (row.get("payer_name") or "").strip()
        plan = (row.get("plan_name") or "").strip()
        if payer:
            payers[f"{payer} | {plan}"] += 1
    return payers


def sample(limit: int = 6) -> None:
    """Download the smallest usable files and check payer-name overlap across systems."""
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    rows = [r for r in csv.DictReader(PROBED_CSV.open(encoding="utf-8"))
            if r.get("status") == "200" and r.get("format") in ("json", "csv")]

    def sort_key(r):
        try:
            return float(r["size_mb"])
        except (ValueError, KeyError):
            return float("inf")

    # One per system first, so overlap is actually testable across systems.
    by_system, picked = defaultdict(list), []
    for row in sorted(rows, key=sort_key):
        by_system[row["system"]].append(row)
    for system, group in by_system.items():
        picked.append(group[0])
    picked = picked[:limit]

    per_system_payers = {}
    for row in picked:
        fname = f"{row['system'].replace(' ', '_')}_{row['format']}"
        dest = DOWNLOADS / f"{fname}.{row['format']}"
        if not dest.exists():
            print(f"  downloading {row['location'][:45]} ({row['size_mb']} MB)...")
            with requests.get(row["mrf_url"], headers=UA, timeout=300, stream=True) as resp:
                resp.raise_for_status()
                with dest.open("wb") as fh:
                    for chunk in resp.iter_content(1 << 20):
                        fh.write(chunk)
        payers = payers_from_json(dest) if row["format"] == "json" else payers_from_csv(dest)
        per_system_payers[row["system"]] = payers
        print(f"  {row['system']}: {len(payers)} distinct payer|plan combinations")
        for label, count in payers.most_common(8):
            print(f"      {count:>8,}  {label}")

    print("\n=== THE GATE ===")
    systems = list(per_system_payers)
    for i, a in enumerate(systems):
        for b in systems[i + 1:]:
            names_a = {p.split("|")[0].strip().lower() for p in per_system_payers[a]}
            names_b = {p.split("|")[0].strip().lower() for p in per_system_payers[b]}
            shared = names_a & names_b
            print(f"{a} vs {b}: {len(shared)} exact payer-name matches")
            if shared:
                for name in sorted(shared)[:5]:
                    print(f"    + {name}")

    print(
        "\nExact matches are a floor, not the answer -- 'BCBS' vs 'Blue Cross Blue "
        "Shield of Illinois' is the same payer written two ways. That normalization\n"
        "work is legitimate engineering depth for the proposal. What kills the project\n"
        "is finding no plausible overlap at all, even fuzzily."
    )
    (OUT / "payers.json").write_text(
        json.dumps({k: dict(v) for k, v in per_system_payers.items()}, indent=2)
    )


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "discover"
    {"discover": discover, "probe": probe,
     "sample": lambda: sample(int(sys.argv[2]) if len(sys.argv) > 2 else 6)}[cmd]()
