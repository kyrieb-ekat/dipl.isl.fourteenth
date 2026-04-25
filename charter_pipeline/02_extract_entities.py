"""
Step 2: Extract structured entities from charter text segments using the Claude API.

Usage:
    python 02_extract_entities.py --vol 1
    python 02_extract_entities.py --vol 1 --start 50 --end 100   # resume / slice

Reads:  output/segments/vol{N}/*.txt + charter_index.csv
Writes: output/entities/vol{N}_raw_entities.json  (appended incrementally)

Requires: ANTHROPIC_API_KEY environment variable.
"""

import argparse
import csv
import json
import os
import sys
from pathlib import Path

import anthropic

sys.path.insert(0, str(Path(__file__).parent))
from config import SEGMENTS_DIR, ENTITIES_DIR, MODEL, MAX_TOKENS, BATCH_SIZE

# ── System prompt (will be cached) ─────────────────────────────────────────
SYSTEM_PROMPT = """You are an expert in medieval Icelandic documentary history specialising in the Diplomatarium Islandicum (DI) — the published edition of Icelandic medieval charters (bréf) and related documents from the 9th to 16th centuries.

Your task is to extract structured data from charter transcriptions. Each charter may be in Old/Middle Icelandic, Medieval Latin, or a mixture. Apply the following rules:

PERSONS
- Extract every named individual with their role in the document.
- Use these role_category values (extend with similar dot-notation if needed):
    issuer-priest, issuer-layman, issuer-bishop, issuer-lawman,
    recipient, principal-opponent,
    witness-testimony, witness-boundary, witness-sealing-priest, witness-sealing-layman,
    scribe, notary, saint-patron
- Record patronymics as part of the name (e.g. "Jón Koðrason", not "Jón" + "Koðrason").
- For clergy, include their see or title as a qualifier if stated (e.g. qualifier: "Bishop of Hólar").
- If the scribe is identified by a later source (e.g. Stefán Karlsson), record that in scribe_source.

DATES
- Express dates in ISO-like format: YYYY-MM-DD, YYYY-MM, or YYYY for partial dates.
- If only a regnal year or feast day is given, convert where possible and note uncertainty.

LOCATIONS
- Distinguish loc.writing (where the document was issued/written) from loc.hearing (where proceedings took place).
- Include region/district qualifiers when stated (e.g. "Skagafjörður").
- List ALL place names mentioned in the body of the document in all_places_mentioned.

DOCUMENT TYPE & CONTENT
- doc_type: short label, e.g. "episcopal judgement", "land sale", "debt settlement", "property transfer", "boundary agreement", "letter of testimony", "máldagi" (church inventory).
- subject: 1–2 sentences describing what the document is about.
- outcome: key result or decision stated in the document (null if no clear resolution).

DI REFERENCE
- The DI fascicle/number reference is usually stated in the editorial apparatus (e.g. "DI II nr. 484" or "Fas. I, 7"). Extract it if present.

Respond ONLY with a valid JSON object matching this schema — no prose, no markdown fences:
{
  "date": "YYYY-MM-DD | YYYY-MM | YYYY | null",
  "date_uncertain": false,
  "scribe": "name or null",
  "scribe_source": "e.g. Stefán Karlsson 1963 [EA A 7] or null",
  "persons": [
    {
      "name": "canonical name",
      "role_category": "role from list above",
      "qualifier": "e.g. Bishop of Hólar or null"
    }
  ],
  "locations": [
    {
      "name": "place name",
      "role": "loc.writing | loc.hearing | loc.mentioned",
      "region": "district/region or null"
    }
  ],
  "all_places_mentioned": ["place1", "place2"],
  "di_reference": "DI X nr. Y or null",
  "doc_type": "type label or null",
  "subject": "1-2 sentence summary or null",
  "outcome": "key outcome or null",
  "seal_info": "description of seal condition/count or null",
  "language": "Icelandic | Latin | Mixed"
}
"""


def load_index(vol_dir: Path) -> list[dict]:
    index_path = vol_dir / "charter_index.csv"
    if not index_path.exists():
        raise FileNotFoundError(f"charter_index.csv not found in {vol_dir}. Run 01_extract_text.py first.")
    with open(index_path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_existing_results(out_path: Path) -> dict[str, dict]:
    """Load already-processed charters so we can resume without re-calling the API."""
    if not out_path.exists():
        return {}
    with open(out_path, encoding="utf-8") as f:
        data = json.load(f)
    return {item["filename"]: item for item in data}


def save_results(results: list[dict], out_path: Path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)


def extract_charter(client: anthropic.Anthropic, charter_text: str) -> dict:
    """Call Claude API for a single charter, returning parsed JSON."""
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},  # cache the system prompt
            }
        ],
        messages=[
            {
                "role": "user",
                "content": f"Extract structured data from this DI charter transcription:\n\n{charter_text}",
            }
        ],
    )
    raw = response.content[0].text.strip()
    # Strip accidental markdown fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw)


def main():
    parser = argparse.ArgumentParser(description="Extract entities from charter segments using Claude API.")
    parser.add_argument("--vol", type=int, required=True, help="Volume number to process.")
    parser.add_argument("--start", type=int, default=1, help="First charter sequence number (1-based). Default: 1.")
    parser.add_argument("--end", type=int, default=None, help="Last charter sequence number (inclusive). Default: all.")
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY environment variable not set.", file=sys.stderr)
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    vol_dir = SEGMENTS_DIR / f"vol{args.vol:02d}"
    out_path = ENTITIES_DIR / f"vol{args.vol:02d}_raw_entities.json"

    index = load_index(vol_dir)
    existing = load_existing_results(out_path)
    results = list(existing.values())

    # Filter to requested range
    charters_to_process = [
        row for row in index
        if args.start <= int(row["sequence"]) <= (args.end or len(index))
        and row["filename"] not in existing
    ]

    print(f"[vol {args.vol}] {len(charters_to_process)} charters to process "
          f"({len(existing)} already done, will resume).")

    for i, row in enumerate(charters_to_process, start=1):
        txt_path = vol_dir / row["filename"]
        if not txt_path.exists():
            print(f"  [{i}/{len(charters_to_process)}] SKIP (file missing): {row['filename']}")
            continue

        charter_text = txt_path.read_text(encoding="utf-8")
        print(f"  [{i}/{len(charters_to_process)}] Processing {row['filename']} …", end=" ", flush=True)

        try:
            extracted = extract_charter(client, charter_text)
        except json.JSONDecodeError as e:
            print(f"PARSE ERROR — {e}")
            extracted = {"_parse_error": str(e)}
        except Exception as e:
            print(f"API ERROR — {e}")
            extracted = {"_api_error": str(e)}

        result = {
            "filename": row["filename"],
            "volume": int(row["volume"]),
            "sequence": int(row["sequence"]),
            "page_start": int(row["page_start"]),
            "date_header": row["date_header"],
            **extracted,
        }
        results.append(result)
        print("OK")

        # Save after every charter so progress is not lost
        if i % BATCH_SIZE == 0 or i == len(charters_to_process):
            save_results(results, out_path)
            print(f"    → Saved {len(results)} total results to {out_path.name}")

    print(f"\nDone. Results in {out_path}")


if __name__ == "__main__":
    main()
