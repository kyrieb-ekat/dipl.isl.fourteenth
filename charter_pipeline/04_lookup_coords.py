"""
Step 4: Look up coordinates for new/ungeocoded places via Wikidata SPARQL.

Usage:
    python 04_lookup_coords.py --vol 1
    python 04_lookup_coords.py --vol 1 --country Q189   # Q189 = Iceland (default)

Reads:  output/review/vol{N}_places_new.csv  (produced by 05_export_csvs.py)
        OR you can pass --places-csv directly for standalone use.

Writes: output/review/vol{N}_places_new_geocoded.csv
        — same file with coordinates filled where Wikidata matched confidently
        — a match_score column lets you filter/verify before merging

Notes:
  - Only places with empty coordinates_lat are queried.
  - Matches with fuzzy score < 70 are left blank (coordinates_lat = "REVIEW").
  - No coordinates are written without your manual approval (use 06_merge_into_xlsx.py).
"""

import argparse
import csv
import sys
import time
from pathlib import Path

from rapidfuzz import fuzz, process
from SPARQLWrapper import JSON, SPARQLWrapper

sys.path.insert(0, str(Path(__file__).parent))
from config import REVIEW_DIR

WIKIDATA_ENDPOINT = "https://query.wikidata.org/sparql"
GEO_ACCEPT_SCORE = 70  # fuzzy threshold for auto-accepting a Wikidata match


def query_iceland_places() -> list[dict]:
    """
    Fetch all Icelandic places with coordinates from Wikidata.
    Returns list of {label, alt_labels, lat, long, wikidata_id}.
    Results are cached in memory for the duration of the run.
    """
    sparql = SPARQLWrapper(WIKIDATA_ENDPOINT)
    sparql.addCustomHttpHeader("User-Agent", "DiplomatariumIslandicumPipeline/1.0 (research; contact: researcher)")
    sparql.setReturnFormat(JSON)
    sparql.setQuery("""
        SELECT DISTINCT ?place ?placeLabel (GROUP_CONCAT(?altLabel; separator="|") AS ?altLabels)
               ?lat ?long WHERE {
          ?place wdt:P17 wd:Q189 .
          ?place wdt:P625 ?coords .
          OPTIONAL { ?place skos:altLabel ?altLabel . FILTER(LANG(?altLabel) IN ("is","en")) }
          BIND(geof:latitude(?coords)  AS ?lat)
          BIND(geof:longitude(?coords) AS ?long)
          SERVICE wikibase:label { bd:serviceParam wikibase:language "is,en". }
        }
        GROUP BY ?place ?placeLabel ?lat ?long
    """)

    print("Querying Wikidata for Icelandic places … (this may take ~30s)")
    results = sparql.query().convert()
    bindings = results["results"]["bindings"]
    places = []
    for b in bindings:
        label = b.get("placeLabel", {}).get("value", "")
        alt = b.get("altLabels", {}).get("value", "")
        lat  = b.get("lat", {}).get("value", "")
        long = b.get("long", {}).get("value", "")
        wdid = b.get("place", {}).get("value", "").split("/")[-1]
        if lat and long:
            places.append({
                "label": label,
                "alt_labels": alt,
                "all_labels": (label + "|" + alt).lower(),
                "lat": lat,
                "long": long,
                "wikidata_id": wdid,
            })
    print(f"  Retrieved {len(places)} Icelandic places from Wikidata.")
    return places


def match_place(name: str, wd_places: list[dict]) -> tuple[str, str, str, int]:
    """
    Fuzzy-match a place name against all Wikidata labels.
    Returns (lat, long, wikidata_id, score). Returns ("", "", "", 0) on no match.
    """
    if not wd_places:
        return "", "", "", 0

    candidates = [p["all_labels"] for p in wd_places]
    matched_key, score, idx = process.extractOne(
        name.lower(), candidates, scorer=fuzz.token_sort_ratio
    )
    best = wd_places[idx]
    return best["lat"], best["long"], best["wikidata_id"], int(score)


def main():
    parser = argparse.ArgumentParser(description="Geocode new places via Wikidata SPARQL.")
    parser.add_argument("--vol", type=int, help="Volume number (used to find places CSV automatically).")
    parser.add_argument("--places-csv", type=Path, help="Direct path to a places CSV to geocode.")
    args = parser.parse_args()

    if args.places_csv:
        in_path = args.places_csv
    elif args.vol:
        in_path = REVIEW_DIR / f"vol{args.vol:02d}_places_new.csv"
    else:
        print("Error: provide --vol or --places-csv.", file=sys.stderr)
        sys.exit(1)

    if not in_path.exists():
        print(f"Error: {in_path} not found. Run 05_export_csvs.py first.", file=sys.stderr)
        sys.exit(1)

    with open(in_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    ungeocoded = [r for r in rows if not r.get("coordinates_lat", "").strip()]
    if not ungeocoded:
        print("All places already have coordinates. Nothing to do.")
        return

    print(f"{len(ungeocoded)} places need coordinates.")
    wd_places = query_iceland_places()

    matched_count = 0
    for row in rows:
        if row.get("coordinates_lat", "").strip():
            row["wikidata_id"] = row.get("wikidata_id", "")
            row["geo_match_score"] = ""
            continue

        lat, long, wdid, score = match_place(row["canonical_name"], wd_places)
        if score >= GEO_ACCEPT_SCORE:
            row["coordinates_lat"]  = lat
            row["coordinates_long"] = long
            row["wikidata_id"]      = wdid
            row["geo_match_score"]  = score
            matched_count += 1
        else:
            row["coordinates_lat"]  = ""
            row["coordinates_long"] = ""
            row["wikidata_id"]      = wdid if wdid else ""
            row["geo_match_score"]  = score if score else ""

        time.sleep(0.05)  # be polite to Wikidata

    out_path = in_path.with_name(in_path.stem + "_geocoded.csv")
    fieldnames = list(rows[0].keys())
    for extra in ["wikidata_id", "geo_match_score"]:
        if extra not in fieldnames:
            fieldnames.append(extra)

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Geocoded {matched_count}/{len(ungeocoded)} places automatically.")
    print(f"Remaining {len(ungeocoded) - matched_count} need manual lookup.")
    print(f"Review: {out_path}")
    print("\nTip: Filter geo_match_score < 85 for careful manual verification.")


if __name__ == "__main__":
    main()
