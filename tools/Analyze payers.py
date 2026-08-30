"""
 
from __future__ import annotations
 
import json
import re
from collections import defaultdict
from pathlib import Path
 
PAYERS_JSON = Path("gate_output/payers.json")
 
# Rate types CMS requires in the file that are not insurers.
NON_PAYER = re.compile(
    r"gross|cash|self[\s_-]*pay|uninsured|list price|charge master|chargemaster"
    r"|de[\s_-]*identified|min(imum)?$|max(imum)?$|^n/?a$|^unknown$|^\?$",
    re.I,
)
 
# Canonical payer -> patterns that mean it. Order matters; first hit wins.
# Extend this as you see unmatched names in the report at the bottom.
CANON: list[tuple[str, str]] = [
    ("Blue Cross Blue Shield", r"\bbcbs|\bbcbsil|blue\s*cross|bluecross|\bhcsc\b|\bbcs\b|\bbc\b"),
    ("UnitedHealthcare",       r"united\s*health|\buhc\b|\bumr\b|optum|river\s*valley"),
    ("Aetna",                  r"\baetna\b|\baet\b|\bahp\b"),
    ("Cigna",                  r"\bcigna\b|\bcig\b|great\s*west"),
    ("Humana",                 r"\bhumana\b|\bhum\b|\bchoicecare\b"),
    ("Medicare",               r"medicare|\bcms\b|\bmcr\b"),
    ("Medicaid",               r"medicaid|\bhfs\b|\bmcd\b"),
    ("Meridian",               r"meridian"),
    ("Molina",                 r"molina"),
    ("Centene / Ambetter",     r"centene|ambetter|celticare"),
    ("Health Alliance",        r"health\s*alliance|\bhami\b"),
    ("MultiPlan / PHCS",       r"multiplan|\bphcs\b|private\s*health\s*care"),
    ("Tricare",                r"tricare|champus|\bva\b\s*(community|ccn)"),
    ("Workers Compensation",   r"work(ers)?\s*comp|\bwc\b"),
    ("County Care",            r"county\s*care|countycare"),
    ("Aetna Better Health",    r"aetna\s*better"),
]
 
 
def canonicalize(raw: str) -> str | None:
    """Map a raw payer string onto a canonical insurer, or None if not a payer."""
    name = re.sub(r"[^a-z0-9\s]", " ", raw.lower())
    name = re.sub(r"\s+", " ", name).strip()
    if not name or NON_PAYER.search(name):
        return None
    for canon, pattern in CANON:
        if re.search(pattern, name):
            return canon
    return f"UNMATCHED: {raw.strip()}"
 
 
def main() -> None:
    if not PAYERS_JSON.exists():
        raise SystemExit(f"{PAYERS_JSON} not found -- run `chicago_mrf_gate.py sample` first")
 
    data = json.loads(PAYERS_JSON.read_text())
 
    # ---- 1. Diagnostic: did every file actually parse? -------------------
    print("=" * 68)
    print("PARSE DIAGNOSTIC  (wildly uneven counts = a parser problem)")
    print("=" * 68)
    print(f"{'system':<26}{'raw payer|plan':>16}{'distinct payer':>18}{'records':>12}")
    raw_payer_names: dict[str, set[str]] = {}
    for system, combos in sorted(data.items()):
        names = {c.split("|")[0].strip() for c in combos}
        raw_payer_names[system] = names
        print(f"{system:<26}{len(combos):>16,}{len(names):>18,}{sum(combos.values()):>12,}")
 
    counts = [len(v) for v in raw_payer_names.values()]
    if counts and max(counts) > 5 * max(min(counts), 1):
        print("\n  !! One or more systems yielded far fewer payers than the others.")
        print("     Inspect those files by hand before trusting any overlap number.")
 
    # ---- 2. Canonical mapping -------------------------------------------
    by_canon: dict[str, set[str]] = defaultdict(set)
    dropped: dict[str, set[str]] = defaultdict(set)
    unmatched: dict[str, set[str]] = defaultdict(set)
 
    for system, names in raw_payer_names.items():
        for raw in names:
            canon = canonicalize(raw)
            if canon is None:
                dropped[system].add(raw)
            elif canon.startswith("UNMATCHED: "):
                unmatched[system].add(raw)
            else:
                by_canon[canon].add(system)
 
    systems = sorted(raw_payer_names)
 
    print("\n" + "=" * 68)
    print("CANONICAL PAYER COVERAGE")
    print("=" * 68)
    width = max((len(c) for c in by_canon), default=10) + 2
    header = "".join(s[:11].rjust(13) for s in systems)
    print(f"{'payer':<{width}}{header}{'  systems':>10}")
    shared_by_all, shared_by_two = [], []
    for canon in sorted(by_canon, key=lambda c: (-len(by_canon[c]), c)):
        present = by_canon[canon]
        marks = "".join(("  YES" if s in present else "    -").rjust(13) for s in systems)
        print(f"{canon:<{width}}{marks}{len(present):>10}")
        if len(present) == len(systems):
            shared_by_all.append(canon)
        elif len(present) >= 2:
            shared_by_two.append(canon)
 
    # ---- 3. The verdict --------------------------------------------------
    print("\n" + "=" * 68)
    print("THE GATE")
    print("=" * 68)
    print(f"payers present in ALL {len(systems)} systems : {len(shared_by_all)}")
    for c in shared_by_all:
        print(f"    + {c}")
    print(f"payers present in 2+ systems         : {len(shared_by_two) + len(shared_by_all)}")
 
    if len(shared_by_all) >= 2:
        print("\n  PASS. Cross-facility comparison works. Build against the payers")
        print("  shared by all systems and treat the rest as future scope.")
    elif shared_by_two:
        print("\n  PARTIAL. Comparison works pairwise but not across all four.")
        print("  Either narrow to the systems that do share payers, or resolve the")
        print("  parse diagnostic above first -- it may be hiding real overlap.")
    else:
        print("\n  FAIL as measured. Check the diagnostic and UNMATCHED list below")
        print("  before concluding anything -- this is more likely a parsing or")
        print("  naming problem than a genuine absence of shared contracts.")
 
    if unmatched:
        print("\n" + "-" * 68)
        print("UNMATCHED NAMES -- add rules to CANON for any real insurer here")
        print("-" * 68)
        for system in systems:
            names = sorted(unmatched.get(system, ()))
            if names:
                print(f"\n{system} ({len(names)}):")
                for n in names[:25]:
                    print(f"    {n}")
                if len(names) > 25:
                    print(f"    ... {len(names) - 25} more")
 
    if dropped:
        print("\n" + "-" * 68)
        print("EXCLUDED AS NON-PAYER RATE TYPES (expected, not a problem)")
        print("-" * 68)
        for system in systems:
            names = sorted(dropped.get(system, ()))
            if names:
                print(f"  {system}: {', '.join(names[:6])}"
                      + (f" ... +{len(names) - 6}" if len(names) > 6 else ""))
 
    Path("gate_output/canonical_payers.json").write_text(
        json.dumps({c: sorted(s) for c, s in by_canon.items()}, indent=2)
    )
    print("\n-> gate_output/canonical_payers.json")
 
 
if __name__ == "__main__":
    main()
 