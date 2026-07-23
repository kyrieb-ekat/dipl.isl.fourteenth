"""
Step 4a: Reconcile place mentions against nafnid.is (Árnastofnun)
farm/settlement records, as a supplementary source alongside the Wikidata
lookup in 04_lookup_coords.py.

Usage:
    python 04a_reconcile_nafnid.py --vol 1
    python 04a_reconcile_nafnid.py --vol 4 --top-n 3
    python 04a_reconcile_nafnid.py --vol 4 --ungeocoded-only

Reads:  places table (status='provisional', source_volume=N) in charter_pipeline.db
Writes: place_duplicate_candidates table for that volume's places
        — one row per (DI place, candidate rank) pair, blank `decision`
        column for manual triage (Place Duplicates tab). Never auto-accepts
        a match; a confirmed 'same' decision backfills places.nafnid_id
        (see db.record_place_duplicate_decision()) but does not promote
        anything on its own -- promotion still goes through Final Review.

Strategy:
  1. Normalize both sides (case, whitespace, punctuation; fold accents
     while keeping þ/ð/æ/ö as real letters, since those aren't accent
     variants, they're distinct letters).
  2. Block candidates by sýsla before fuzzy matching, to cut down false
     positives from repeated farm names (Aðalból, Garður, Gafl, etc.
     all occur more than once nationally). DI-style abbreviations
     (Hún., Skag., ...) are expanded via lookup_tables/sysla_abbrevs.csv
     into nafnid's full modern sýsla name(s) — including both halves of
     a sýsla split in 1907 — before blocking, since nafnid only stores
     full modern names. Falls back to the full place list when no
     sýsla is known or recognized — flag those rows for extra scrutiny
     during review.
  3. Score name similarity with rapidfuzz.fuzz.WRatio, top-N per mention.
  4. Wikidata is NOT the only source of truth here: nafnid may know
     places Wikidata never catalogued, so by default every place is
     reconciled against nafnid, not just the ones Wikidata left
     ungeocoded (pass --ungeocoded-only to restore that narrower scope).
  5. Wherever a DI mention already has coordinates (from Wikidata),
     also search nafnid geographically (haversine distance, independent
     of name similarity) — coordinate proximity is a much stronger
     duplicate signal than name matching alone, and surfaces both
     candidates name-matching would rank low (`geo_only`) and cases
     where a strong name match is geographically implausible
     (`geo_conflict`). Proximity alone never earns `geo_confirmed`,
     though — a city or country mention gets one representative point,
     and plenty of unrelated farms legitimately sit within a few km of
     it, so `geo_confirmed` also requires a plausible name score.
"""

import argparse
import csv
import re
import sys
import unicodedata
from dataclasses import dataclass
from math import atan2, cos, radians, sin, sqrt
from pathlib import Path

from rapidfuzz import fuzz, process

sys.path.insert(0, str(Path(__file__).parent))
from config import NAFNID_DATA_DIR, NAFNID_LOOKUP_DIR
import db

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


def try_float(v) -> float | None:
    try:
        if v is None or str(v).strip() == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlambda = radians(lon2 - lon1)
    a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlambda / 2) ** 2
    return 2 * r * atan2(sqrt(a), sqrt(1 - a))


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


def load_sysla_crosswalk(path: Path) -> dict:
    """DI charters use historical abbreviations (Hún., Skag., S-Þing., ...);
    nafnid's baeir.csv stores full modern sýsla names. A single DI-era
    sýsla can map to more than one modern name where an 1907 (or later)
    split occurred — full_names_modern is semicolon-separated for those.
    Returns a dict keyed by both the abbreviation as-given and with any
    trailing period stripped, so lookups don't need to guess formatting."""
    crosswalk = {}
    if not path.exists():
        return crosswalk
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            abbr = (row.get("abbreviation") or "").strip()
            fulls = [x.strip() for x in (row.get("full_names_modern") or "").split(";") if x.strip()]
            if not abbr or not fulls:
                continue
            crosswalk[abbr] = fulls
            crosswalk[abbr.rstrip(".")] = fulls
    return crosswalk


def expand_sysla(label: str, crosswalk: dict) -> list:
    """Resolves a DI-side sýsla label to the nafnid full-name form(s) to
    block on. Falls back to treating the label as already-a-full-name if
    it isn't a recognized abbreviation — harmless if that guess is wrong,
    since callers fall back to the unblocked full place list when the
    resulting pool comes up empty."""
    label = (label or "").strip()
    if not label:
        return []
    return crosswalk.get(label, [label])


def load_di_mentions(volume: int, ungeocoded_only: bool = False) -> list:
    """Adapts the places table's rows (place_pk, canonical_name,
    coordinates_lat/long, region, district, ...) into the shape reconcile()
    expects. By default carries through EVERY provisional place for this
    volume, not just ones Wikidata left ungeocoded — nafnid isn't just a
    fallback for Wikidata's misses, it may know places Wikidata never
    catalogued at all, so Wikidata-confirmed places still get a second
    opinion (pass ungeocoded_only=True to restore the narrower scope)."""
    df = db.get_places(status="provisional", source_volume=volume)
    mentions = []
    for r in df.to_dict("records"):
        di_lat = try_float(r.get("coordinates_lat"))
        di_lng = try_float(r.get("coordinates_long"))
        wikidata_status = "geocoded" if di_lat is not None else "ungeocoded"
        if ungeocoded_only and wikidata_status == "geocoded":
            continue
        mentions.append({
            "name": r.get("canonical_name", ""),
            "sysla": r.get("district") or r.get("region") or "",
            "_place_id": r["place_pk"],
            "_lat": di_lat,
            "_lng": di_lng,
            "_wikidata_status": wikidata_status,
            "_place_type": r.get("place_type", ""),
            "_region": r.get("region") or r.get("modern_equivalent") or "",
        })
    return mentions


# ── Matching ─────────────────────────────────────────────────────────────


def build_sysla_index(places):
    index = {}
    for p in places:
        index.setdefault(p.sysla, []).append(p)
    return index


def pool_for_sysla(target_sysla: str, index: dict, crosswalk: dict, all_places: list) -> list:
    if not target_sysla:
        return all_places
    names = expand_sysla(target_sysla, crosswalk)
    pool, seen = [], set()
    for n in names:
        for p in index.get(n, []):
            if p.id not in seen:
                pool.append(p)
                seen.add(p.id)
    return pool if pool else all_places  # unrecognized/empty sysla - fall back, flag it


def name_candidates(name: str, pool: list, top_n: int = 5):
    norm_target = normalize_name(name, fold_accents=True)
    choices = {p.id: normalize_name(p.name, fold_accents=True) for p in pool}
    if not choices:
        return []
    results = process.extract(norm_target, choices, scorer=fuzz.WRatio, limit=top_n)
    by_id = {p.id: p for p in pool}
    return [(by_id[match_id], score) for _, score, match_id in results]


def geo_candidates(lat: float, lng: float, pool: list, top_n: int = 5, max_km: float = 15.0):
    scored = []
    for p in pool:
        p_lat, p_lng = try_float(p.lat), try_float(p.lng)
        if p_lat is None or p_lng is None:
            continue
        d = haversine_km(lat, lng, p_lat, p_lng)
        if d <= max_km:
            scored.append((p, d))
    scored.sort(key=lambda x: x[1])
    return scored[:top_n]


def reconcile(di_mentions, places, crosswalk, sysla_field="sysla", name_field="name",
              top_n=5, max_km=15.0, confirm_km=5.0, conflict_km=25.0, confirm_name_floor=60.0):
    """If a DI mention doesn't carry a recognized sýsla, falls back to
    matching against the full place list (slower, noisier — flag these
    rows for extra scrutiny in review). Surfaces name-based and
    coordinate-based candidates side by side rather than blending them
    into one score, so a human reviewer can weigh disagreement between
    the two signals themselves.

    Proximity alone is NOT sufficient to call a match "confirmed": a
    capital city or whole-country mention gets assigned a single
    representative coordinate, and plenty of unrelated farms legitimately
    sit within a few km of it without being the same place. geo_confirmed
    therefore requires BOTH a close distance AND a plausible name score;
    close-but-name-mismatched candidates are labeled geo_only instead —
    still worth a look (this is exactly the case where orthographic drift
    could hide a real match from name-matching alone), just not asserted
    as confirmed."""
    index = build_sysla_index(places)
    rows_out = []
    for mention in di_mentions:
        target_name = mention.get(name_field, "")
        target_sysla = mention.get(sysla_field, "")
        place_id = mention.get("_place_id", "")
        di_lat, di_lng = mention.get("_lat"), mention.get("_lng")
        wikidata_status = mention.get("_wikidata_status", "")
        di_place_type = mention.get("_place_type", "")
        di_region = mention.get("_region", "")

        pool = pool_for_sysla(target_sysla, index, crosswalk, places)

        merged = {}  # candidate_id -> {place, name_score, distance_km, sources}
        for place, score in name_candidates(target_name, pool, top_n=top_n):
            merged[place.id] = {"place": place, "name_score": round(score, 1),
                                 "distance_km": None, "sources": {"name"}}

        if di_lat is not None and di_lng is not None:
            geo_matches = geo_candidates(di_lat, di_lng, pool, top_n=top_n, max_km=max_km)
            if not geo_matches and pool is not places:
                geo_matches = geo_candidates(di_lat, di_lng, places, top_n=top_n, max_km=max_km)
            for place, dist in geo_matches:
                entry = merged.setdefault(place.id, {"place": place, "name_score": None,
                                                       "distance_km": None, "sources": set()})
                entry["distance_km"] = round(dist, 2)
                entry["sources"].add("geo")

        # backfill a name score for geo-only candidates - cheap, always computable
        for entry in merged.values():
            if entry["name_score"] is None:
                score = fuzz.WRatio(normalize_name(target_name, True),
                                     normalize_name(entry["place"].name, True))
                entry["name_score"] = round(score, 1)

        if not merged:
            rows_out.append({
                "place_id": place_id, "di_name": target_name, "di_sysla_given": target_sysla,
                "di_place_type": di_place_type, "di_region": di_region,
                "wikidata_status": wikidata_status, "candidate_rank": "", "name_score": "",
                "distance_km": "", "flag": "", "match_sources": "",
                "candidate_name": "NO MATCH FOUND", "candidate_id": "", "candidate_hreppur": "",
                "candidate_sysla": "", "candidate_lat": "", "candidate_lng": "", "decision": "",
            })
            continue

        ranked = sorted(merged.values(),
                         key=lambda e: (e["distance_km"] if e["distance_km"] is not None else 9e9,
                                         -e["name_score"]))[:max(top_n, 1)]

        for rank, entry in enumerate(ranked, start=1):
            place, name_score, dist = entry["place"], entry["name_score"], entry["distance_km"]
            flag = ""
            if dist is not None and dist <= confirm_km and name_score >= confirm_name_floor:
                flag = "geo_confirmed"
            elif dist is not None and dist > conflict_km and name_score >= 80:
                flag = "geo_conflict"
            elif dist is not None and dist <= confirm_km:
                flag = "geo_only"  # close by, but name doesn't clearly match - worth a look
            rows_out.append({
                "place_id": place_id,
                "di_name": target_name,
                "di_sysla_given": target_sysla,
                "di_place_type": di_place_type,
                "di_region": di_region,
                "wikidata_status": wikidata_status,
                "candidate_rank": rank,
                "name_score": name_score,
                "distance_km": dist if dist is not None else "",
                "flag": flag,
                "match_sources": "+".join(sorted(entry["sources"])),
                "candidate_name": place.name,
                "candidate_id": place.id,
                "candidate_hreppur": place.hreppur,
                "candidate_sysla": place.sysla,
                "candidate_lat": place.lat,
                "candidate_lng": place.lng,
                "decision": "",  # blank column for manual accept/reject during review
            })
    return rows_out


def _num_or_none(v):
    return v if v not in ("", None) else None


def save_candidates(volume: int, rows: list[dict]) -> int:
    """Replaces every place_duplicate_candidates row for this volume's
    places (04a's re-run behavior -- no confirmed-same signal exists for
    places, so nothing needs preserving across a re-run, unlike persons)."""
    if not rows:
        print("No rows to write")
        return 0
    db_rows = []
    for r in rows:
        if not r.get("candidate_id"):
            continue  # skip "NO MATCH FOUND" placeholder rows -- nothing to record
        db_rows.append({
            "place_pk": r["place_id"],
            "di_name": r["di_name"], "di_sysla_given": r["di_sysla_given"],
            "di_place_type": r["di_place_type"], "di_region": r["di_region"],
            "wikidata_status": r["wikidata_status"],
            "candidate_rank": _num_or_none(r["candidate_rank"]),
            "name_score": _num_or_none(r["name_score"]),
            "distance_km": _num_or_none(r["distance_km"]),
            "flag": r["flag"], "match_sources": r["match_sources"],
            "candidate_name": r["candidate_name"], "candidate_nafnid": r["candidate_id"],
            "candidate_hreppur": r["candidate_hreppur"], "candidate_sysla": r["candidate_sysla"],
            "candidate_lat": _num_or_none(r["candidate_lat"]),
            "candidate_lng": _num_or_none(r["candidate_lng"]),
        })
    db.replace_place_duplicate_candidates(volume, db_rows)
    print(f"Wrote {len(db_rows)} candidate row(s) for vol{volume:02d} to place_duplicate_candidates")
    return len(db_rows)


# ── Run ──────────────────────────────────────────────────────────────────


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vol", required=True, help="volume number, e.g. 1 or 01")
    ap.add_argument("--top-n", type=int, default=5)
    ap.add_argument("--max-km", type=float, default=15.0, help="radius for geo candidate search")
    ap.add_argument("--confirm-km", type=float, default=5.0, help="distance at/under which a match may be flagged geo_confirmed")
    ap.add_argument("--confirm-name-floor", type=float, default=60.0,
                     help="minimum name_score also required for geo_confirmed (proximity alone isn't enough)")
    ap.add_argument("--conflict-km", type=float, default=25.0, help="distance beyond which a high name score is flagged geo_conflict")
    ap.add_argument("--ungeocoded-only", action="store_true",
                     help="only reconcile places Wikidata left ungeocoded (old narrower default)")
    ap.add_argument("--baeir-csv", default=None, help="override the nafnid baeir.csv path")
    args = ap.parse_args()

    volume = int(args.vol)
    baeir_csv = Path(args.baeir_csv) if args.baeir_csv else NAFNID_DATA_DIR / "baeir.csv"

    places = load_baeir(baeir_csv)
    crosswalk = load_sysla_crosswalk(NAFNID_LOOKUP_DIR / "sysla_abbrevs.csv")
    di_mentions = load_di_mentions(volume, ungeocoded_only=args.ungeocoded_only)
    n_geocoded = sum(1 for m in di_mentions if m["_wikidata_status"] == "geocoded")
    print(f"{len(di_mentions)} place(s) from vol{volume:02d} to reconcile against "
          f"{len(places)} nafnid records ({n_geocoded} already Wikidata-geocoded, "
          f"still checked against nafnid unless --ungeocoded-only)")

    rows = reconcile(di_mentions, places, crosswalk, top_n=args.top_n,
                      max_km=args.max_km, confirm_km=args.confirm_km, conflict_km=args.conflict_km,
                      confirm_name_floor=args.confirm_name_floor)
    save_candidates(volume, rows)

    n_confirmed = sum(1 for r in rows if r["flag"] == "geo_confirmed")
    n_conflict = sum(1 for r in rows if r["flag"] == "geo_conflict")
    n_geo_only = sum(1 for r in rows if r["flag"] == "geo_only")
    print(f"Flags: {n_confirmed} geo_confirmed, {n_conflict} geo_conflict, {n_geo_only} geo_only")


if __name__ == "__main__":
    main()
