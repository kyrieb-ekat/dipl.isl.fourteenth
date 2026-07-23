"""
Step 4: Look up coordinates for new/ungeocoded places via Wikidata SPARQL.

Usage:
    python 04_lookup_coords.py --vol 1
    python 04_lookup_coords.py --vol 1 --country Q189   # Q189 = Iceland (default)

Reads:  places table (status='provisional', source_volume=N) in charter_pipeline.db
Writes: same rows, in place, via db.update_place_geocoding()
        — coordinates filled where Wikidata matched confidently
        — geo_match_score lets you filter/verify before promotion

Notes:
  - Only places with empty coordinates_lat are queried.
  - Matches with fuzzy score < 70 are left blank.
  - No coordinates are written without your manual approval (Final Review).
"""

import argparse
import sys
import time
from pathlib import Path

from rapidfuzz import fuzz, process
from SPARQLWrapper import JSON, SPARQLWrapper

sys.path.insert(0, str(Path(__file__).parent))
import db

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
    parser.add_argument("--vol", type=int, required=True, help="Volume number.")
    args = parser.parse_args()

    df = db.get_places(status="provisional", source_volume=args.vol)
    if df.empty:
        print(f"No provisional places found for vol{args.vol:02d}. Run 05_export_csvs.py first.",
              file=sys.stderr)
        sys.exit(1)

    ungeocoded = df[df["coordinates_lat"].isna()]
    if ungeocoded.empty:
        print("All places already have coordinates. Nothing to do.")
        return

    print(f"{len(ungeocoded)} places need coordinates.")
    wd_places = query_iceland_places()

    matched_count = 0
    for row in ungeocoded.to_dict("records"):
        lat, long, wdid, score = match_place(row["canonical_name"], wd_places)
        if score >= GEO_ACCEPT_SCORE:
            db.update_place_geocoding(row["place_pk"], coordinates_lat=float(lat),
                                       coordinates_long=float(long), wikidata_id=wdid,
                                       geo_match_score=score)
            matched_count += 1
        else:
            db.update_place_geocoding(row["place_pk"], wikidata_id=(wdid or None),
                                       geo_match_score=(score or None))
        time.sleep(0.05)  # be polite to Wikidata

    print(f"Geocoded {matched_count}/{len(ungeocoded)} places automatically.")
    print(f"Remaining {len(ungeocoded) - matched_count} need manual lookup.")
    print(f"Updated vol{args.vol:02d} places in charter_pipeline.db.")
    print("\nTip: Filter geo_match_score < 85 for careful manual verification.")


if __name__ == "__main__":
    main()
