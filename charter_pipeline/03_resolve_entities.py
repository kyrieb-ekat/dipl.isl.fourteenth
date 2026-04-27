"""
Step 3: Resolve extracted entity strings against existing authority files.

Usage:
    python 03_resolve_entities.py --vol 1

Reads:
    output/entities/vol{N}_raw_entities.json
    CHARTER_authority_FILE (persons_authority + Places_Authority sheets)

Writes:
    output/entities/vol{N}_resolved_entities.json
        — each charter gets person_ids[], location_ids[], new_persons[], new_places[]
    output/review/vol{N}_review_queue.csv
        — ambiguous matches (score 60-84) for manual inspection
"""

import argparse
import json
import sys
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz, process

sys.path.insert(0, str(Path(__file__).parent))
from config import ENTITIES_DIR, REVIEW_DIR, AUTHORITY_FILE, FUZZY_ACCEPT, FUZZY_REVIEW
from place_authority import PlaceAuthority


def load_authority(sheet: str, id_col: str, name_col: str, variant_col: str) -> pd.DataFrame:
    df = pd.read_excel(AUTHORITY_FILE, sheet_name=sheet, dtype=str).fillna("")
    return df[[id_col, name_col, variant_col]].rename(
        columns={id_col: "id", name_col: "canonical", variant_col: "variants"}
    )


def build_lookup(df: pd.DataFrame) -> dict[str, str]:
    """Return {name_form: id} covering canonical + all variant spellings."""
    lookup = {}
    for _, row in df.iterrows():
        lookup[row["canonical"].strip().lower()] = row["id"]
        for v in row["variants"].split(";"):
            v = v.strip().lower()
            if v:
                lookup[v] = row["id"]
    return lookup


def next_id(existing_ids: list[str], prefix: str) -> str:
    nums = [int(i[1:]) for i in existing_ids if i.startswith(prefix) and i[1:].isdigit()]
    return f"{prefix}{(max(nums) + 1) if nums else 1:03d}"


def fuzzy_match(name: str, lookup: dict[str, str], authority_df: pd.DataFrame):
    """
    Return (id_or_None, score, matched_canonical).
    Uses the RapidFuzz token_sort_ratio for tolerance of word-order variants.
    """
    candidates = list(lookup.keys())
    if not candidates:
        return None, 0, ""

    matched_key, score, _ = process.extractOne(
        name.lower(), candidates, scorer=fuzz.token_sort_ratio
    )
    matched_id = lookup[matched_key]
    # Retrieve canonical name for reporting
    row = authority_df[authority_df["id"] == matched_id].iloc[0]
    return matched_id, score, row["canonical"]


def resolve_persons(
    persons: list[dict],
    persons_df: pd.DataFrame,
    persons_lookup: dict,
    existing_ids: list[str],
    fuzzy_accept: int = FUZZY_ACCEPT,
    fuzzy_review: int = FUZZY_REVIEW,
) -> tuple[list[dict], list[dict], list[dict]]:
    """
    Returns:
        resolved  — [{name, role_category, qualifier, person_id, match_score}]
        new_persons — rows to add to persons_authority (score < FUZZY_REVIEW)
        review_items — ambiguous matches (FUZZY_REVIEW ≤ score < FUZZY_ACCEPT)
    """
    resolved, new_persons, review_items = [], [], []
    seen_new: dict[str, str] = {}  # name → temp_id (avoid duplicating within one charter)

    for p in persons:
        name = p.get("name", "").strip()
        if not name:
            continue

        matched_id, score, canonical = fuzzy_match(name, persons_lookup, persons_df)

        if score >= fuzzy_accept:
            resolved.append({**p, "person_id": matched_id, "match_score": score, "matched_canonical": canonical})
        elif score >= fuzzy_review:
            review_items.append({
                "type": "person", "extracted_name": name,
                "closest_match": canonical, "match_id": matched_id, "score": score,
                "role_category": p.get("role_category", ""),
            })
            resolved.append({**p, "person_id": f"REVIEW:{matched_id}", "match_score": score})
        else:
            # New person
            if name.lower() in seen_new:
                pid = seen_new[name.lower()]
            else:
                pid = next_id(existing_ids + [r["person_id"] for r in new_persons if not r["person_id"].startswith("REVIEW")], "p")
                seen_new[name.lower()] = pid
                new_persons.append({
                    "person_id": pid,
                    "canonical_name": name,
                    "variant_names": "",
                    "patronymic": "",
                    "occupation": p.get("role_category", ""),
                    "title": p.get("qualifier", ""),
                    "floruit_start": "",
                    "floruit_end": "",
                    "gender": "",
                    "associated_places": "",
                    "notes": "",
                    "sources": "",
                    "_extracted_role": p.get("role_category", ""),
                })
            resolved.append({**p, "person_id": pid, "match_score": 0, "matched_canonical": ""})

    return resolved, new_persons, review_items


def resolve_places(
    locations: list[dict],
    all_places: list[str],
    places_df: pd.DataFrame,
    places_lookup: dict,
    existing_ids: list[str],
    place_auth: PlaceAuthority | None = None,
    fuzzy_accept: int = FUZZY_ACCEPT,
    fuzzy_review: int = FUZZY_REVIEW,
) -> tuple[list[dict], list[dict], list[dict]]:
    resolved, new_places, review_items = [], [], []
    seen_new: dict[str, str] = {}

    def resolve_one(name: str, role: str, region: str):
        # Pass 0: check place_names_authority.csv (exact match, highest confidence)
        if place_auth:
            entry = place_auth.lookup(name)
            if entry:
                return {"name": name, "role": role, "region": region,
                        "place_id": entry.place_id, "match_score": 100,
                        "matched_canonical": entry.canonical_name}, None, None

        matched_id, score, canonical = fuzzy_match(name, places_lookup, places_df)
        if score >= fuzzy_accept:
            return {"name": name, "role": role, "region": region,
                    "place_id": matched_id, "match_score": score, "matched_canonical": canonical}, None, None
        elif score >= fuzzy_review:
            review = {"type": "place", "extracted_name": name,
                      "closest_match": canonical, "match_id": matched_id, "score": score, "role": role}
            return {"name": name, "role": role, "region": region,
                    "place_id": f"REVIEW:{matched_id}", "match_score": score}, review, None
        else:
            if name.lower() in seen_new:
                pid = seen_new[name.lower()]
            else:
                pid = next_id(existing_ids + [r["place_id"] for r in new_places], "l")
                seen_new[name.lower()] = pid
                new_places.append({
                    "place_id": pid,
                    "canonical_name": name,
                    "variant_names": "",
                    "place_type": "",
                    "coordinates_lat": "",
                    "coordinates_long": "",
                    "region": region,
                    "district": "",
                    "modern_equivalent": "",
                    "notes": "",
                    "sources": "",
                })
            return {"name": name, "role": role, "region": region,
                    "place_id": pid, "match_score": 0}, None, True

    for loc in locations:
        r, review, _ = resolve_one(loc.get("name", ""), loc.get("role", "loc.mentioned"), loc.get("region", ""))
        resolved.append(r)
        if review:
            review_items.append(review)

    # Also resolve bare place names from all_places_mentioned (loc.mentioned)
    already_named = {loc.get("name", "").lower() for loc in locations}
    for name in all_places:
        if name.lower() not in already_named:
            r, review, _ = resolve_one(name, "loc.mentioned", "")
            resolved.append(r)
            if review:
                review_items.append(review)
            already_named.add(name.lower())

    return resolved, new_places, review_items


def main():
    parser = argparse.ArgumentParser(description="Resolve entities against authority files.")
    parser.add_argument("--vol", type=int, required=True)
    parser.add_argument("--fuzzy-accept", type=int, default=FUZZY_ACCEPT,
                        help=f"Min score to auto-assign an existing ID (default: {FUZZY_ACCEPT})")
    parser.add_argument("--fuzzy-review", type=int, default=FUZZY_REVIEW,
                        help=f"Min score to flag for manual review (default: {FUZZY_REVIEW})")
    args = parser.parse_args()
    fa, fr = args.fuzzy_accept, args.fuzzy_review
    print(f"Thresholds: auto-assign >= {fa}, review >= {fr}, new < {fr}")

    raw_path = ENTITIES_DIR / f"vol{args.vol:02d}_raw_entities.json"
    if not raw_path.exists():
        print(f"Error: {raw_path} not found. Run 02_extract_entities.py first.", file=sys.stderr)
        sys.exit(1)

    with open(raw_path, encoding="utf-8") as f:
        charters = json.load(f)

    persons_df  = load_authority("persons_authority", "person_id", "canonical_name", "variant_names")
    places_df   = load_authority("Places_Authority",  "place_id",  "canonical_name", "variant_names")
    persons_lookup = build_lookup(persons_df)
    places_lookup  = build_lookup(places_df)

    place_auth = PlaceAuthority()  # loads place_names_authority.csv if present

    existing_person_ids = persons_df["id"].tolist()
    existing_place_ids  = places_df["id"].tolist()

    resolved_charters = []
    all_new_persons: list[dict] = []
    all_new_places: list[dict] = []
    all_review_items: list[dict] = []

    for ch in charters:
        if "_parse_error" in ch or "_api_error" in ch:
            resolved_charters.append({**ch, "_skipped": True})
            continue

        # Track new IDs accumulated so far to avoid collisions across charters
        running_person_ids = existing_person_ids + [p["person_id"] for p in all_new_persons]
        running_place_ids  = existing_place_ids  + [p["place_id"]  for p in all_new_places]

        res_persons, new_p, rev_p = resolve_persons(
            ch.get("persons", []), persons_df, persons_lookup, running_person_ids,
            fuzzy_accept=fa, fuzzy_review=fr,
        )
        res_places, new_l, rev_l = resolve_places(
            ch.get("locations", []), ch.get("all_places_mentioned", []),
            places_df, places_lookup, running_place_ids, place_auth,
            fuzzy_accept=fa, fuzzy_review=fr,
        )

        # Add charter source reference to new entries
        source_ref = f"DI vol.{args.vol} seq.{ch.get('sequence','?')} | {ch.get('di_reference','')}"
        for p in new_p:
            p["sources"] = source_ref
        for l in new_l:
            l["sources"] = source_ref

        all_new_persons.extend(new_p)
        all_new_places.extend(new_l)
        all_review_items.extend([{**r, "charter_filename": ch["filename"], "charter_date": ch.get("date", "")}
                                  for r in rev_p + rev_l])

        resolved_charters.append({
            **ch,
            "resolved_persons": res_persons,
            "resolved_locations": res_places,
            "new_persons": [p["person_id"] for p in new_p],
            "new_places": [l["place_id"] for l in new_l],
        })

    # Save resolved JSON
    out_path = ENTITIES_DIR / f"vol{args.vol:02d}_resolved_entities.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(resolved_charters, f, ensure_ascii=False, indent=2)

    # Save review queue CSV
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    review_path = REVIEW_DIR / f"vol{args.vol:02d}_review_queue.csv"
    if all_review_items:
        import csv
        review_fields = ["type", "extracted_name", "closest_match", "match_id", "score",
                         "role_category", "role", "charter_filename", "charter_date"]
        with open(review_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=review_fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(all_review_items)

    print(f"Resolved {len(resolved_charters)} charters.")
    print(f"  {len(all_new_persons)} new persons  |  {len(all_new_places)} new places")
    print(f"  {len(all_review_items)} items flagged for manual review → {review_path}")
    print(f"Saved: {out_path}")


if __name__ == "__main__":
    main()
