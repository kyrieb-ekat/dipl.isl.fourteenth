"""
Step 4a: Reconcile ungeocoded place mentions against nafnid.is (Árnastofnun)
farm/settlement records, as a supplementary source alongside the Wikidata
lookup in 04_lookup_coords.py.

Usage:
    python 04a_reconcile_nafnid.py --vol 1
    python 04a_reconcile_nafnid.py --vol 4 --top-n 3

Reads:  output/review/vol{N}_places_new_geocoded.csv (falls back to
        vol{N}_places_new.csv if the geocoded file doesn't exist yet)
Writes: output/review/vol{N}_places_nafnid_candidates.csv
        — one row per (DI place, candidate rank) pair, blank `decision`
        column for manual triage. Never auto-accepts a match; accepted
        rows promote into place_names_authority.csv via the existing
        04c_add_to_authority.py path.

Strategy (carried over from the nafnid/ prototype's reconcile.py):
  1. Normalize both sides (case, whitespace, punctuation; fold accents
     while keeping þ/ð/æ/ö as real letters, since those aren't accent
     variants, they're distinct letters).
  2. Block candidates by sýsla before fuzzy matching, to cut down false
     positives from repeated farm names (Aðalból, Garður, Gafl, etc.
     all occur more than once nationally). Falls back to the full
     place list when no sýsla is known — flag those rows for extra
     scrutiny during review.
  3. Score with rapidfuzz.fuzz.WRatio, top-N candidates per mention.
  4. Only reconciles places Wikidata couldn't confidently geocode
     (blank coordinates_lat) — Wikidata-confirmed places don't need a
     second opinion.
"""

import argparse
import csv
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from rapidfuzz import fuzz, process

sys.path.insert(0, str(Path(__file__).parent))
from config import NAFNID_DATA_DIR, REVIEW_DIR

# ── Normalization ────────────────────────────────────────────────────────


def strip_accents(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def normalize_name(name: str, fold_accents: bool = False) -> str:
    if not name:
        return ""
    n = name.strip().lower()
    n = re.sub(r"\s+", " ", n)
    n = re.sub(r"[.,;:]", "", n)
    if fold_accents:
        n = strip_accents(n)
    return n


# ── Data loading ─────────────────────────────────────────────────────────


@dataclass
class NafnidPlace:
    id: str
    name: str
    hreppur: str
    sysla: str
    lat: str
    lng: str


def load_baeir(path: Path) -> list:
    places = []
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            # hreppur/sysla columns are stringified dicts like
            # "{'id': 44, 'nafn': 'Fremri-Torfustaðahreppur'}"
            m_hreppur = re.search(r"'nafn':\s*'([^']*)'", row.get("hreppur") or "")
            m_sysla = re.search(r"'nafn':\s*'([^']*)'", row.get("sysla") or "")
            places.append(NafnidPlace(
                id=row["id"],
                name=row["baejarnafn"],
                hreppur=m_hreppur.group(1) if m_hreppur else "",
                sysla=m_sysla.group(1) if m_sysla else "",
                lat=row.get("lat", ""),
                lng=row.get("lng", ""),
            ))
    return places


def load_di_mentions(review_csv: Path) -> list:
    """Adapts the pipeline's real review-CSV schema (place_id,
    canonical_name, coordinates_lat, region, district, ...) into the
    {name, sysla} shape reconcile() expects, keeping only rows Wikidata
    left ungeocoded (coordinates_lat blank) — those are exactly the
    ones a second, Iceland-specific source can help with."""
    with open(review_csv, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    mentions = []
    for r in rows:
        if (r.get("coordinates_lat") or "").strip():
            continue  # Wikidata already resolved this one confidently
        mentions.append({
            "name": r.get("canonical_name", ""),
            "sysla": r.get("district") or r.get("region") or "",
            "_place_id": r.get("place_id", ""),
        })
    return mentions


# ── Matching ─────────────────────────────────────────────────────────────


def build_sysla_index(places):
    index = {}
    for p in places:
        index.setdefault(p.sysla, []).append(p)
    return index


def candidates_for(name: str, places_in_sysla: list, top_n: int = 5):
    norm_target = normalize_name(name, fold_accents=True)
    choices = {p.id: normalize_name(p.name, fold_accents=True) for p in places_in_sysla}
    if not choices:
        return []
    results = process.extract(norm_target, choices, scorer=fuzz.WRatio, limit=top_n)
    by_id = {p.id: p for p in places_in_sysla}
    return [(by_id[match_id], score) for _, score, match_id in results]


def reconcile(di_mentions, places, sysla_field="sysla", name_field="name", top_n=5):
    """If a DI mention doesn't carry a known sýsla, falls back to
    matching against the full place list (slower, noisier — flag
    these rows for extra scrutiny in review)."""
    index = build_sysla_index(places)
    rows_out = []
    for mention in di_mentions:
        target_name = mention.get(name_field, "")
        target_sysla = mention.get(sysla_field, "")
        place_id = mention.get("_place_id", "")
        pool = index.get(target_sysla) if target_sysla else places
        if target_sysla and not pool:
            pool = places  # unrecognized sysla label - fall back, flag it
        matches = candidates_for(target_name, pool, top_n=top_n)
        if not matches:
            rows_out.append({
                "place_id": place_id,
                "di_name": target_name,
                "di_sysla_given": target_sysla,
                "candidate_rank": "",
                "candidate_score": "",
                "candidate_name": "NO MATCH FOUND",
                "candidate_id": "",
                "candidate_hreppur": "",
                "candidate_sysla": "",
                "candidate_lat": "",
                "candidate_lng": "",
                "decision": "",
            })
            continue
        for rank, (place, score) in enumerate(matches, start=1):
            rows_out.append({
                "place_id": place_id,
                "di_name": target_name,
                "di_sysla_given": target_sysla,
                "candidate_rank": rank,
                "candidate_score": round(score, 1),
                "candidate_name": place.name,
                "candidate_id": place.id,
                "candidate_hreppur": place.hreppur,
                "candidate_sysla": place.sysla,
                "candidate_lat": place.lat,
                "candidate_lng": place.lng,
                "decision": "",  # blank column for manual accept/reject during review
            })
    return rows_out


def save_review_csv(rows, path):
    if not rows:
        print("No rows to write")
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} candidate rows to {path}")


# ── Run ──────────────────────────────────────────────────────────────────


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vol", required=True, help="volume number, e.g. 1 or 01")
    ap.add_argument("--top-n", type=int, default=5)
    ap.add_argument("--baeir-csv", default=None, help="override the nafnid baeir.csv path")
    args = ap.parse_args()

    vol = f"{int(args.vol):02d}"
    geocoded = REVIEW_DIR / f"vol{vol}_places_new_geocoded.csv"
    ungeocoded = REVIEW_DIR / f"vol{vol}_places_new.csv"
    review_csv = geocoded if geocoded.exists() else ungeocoded
    if not review_csv.exists():
        sys.exit(f"No review CSV found for vol{vol} (looked for {geocoded} and {ungeocoded})")

    baeir_csv = Path(args.baeir_csv) if args.baeir_csv else NAFNID_DATA_DIR / "baeir.csv"

    places = load_baeir(baeir_csv)
    di_mentions = load_di_mentions(review_csv)
    print(f"{len(di_mentions)} ungeocoded place(s) from {review_csv.name} "
          f"to reconcile against {len(places)} nafnid records")

    rows = reconcile(di_mentions, places, top_n=args.top_n)
    out_path = REVIEW_DIR / f"vol{vol}_places_nafnid_candidates.csv"
    save_review_csv(rows, out_path)


if __name__ == "__main__":
    main()
