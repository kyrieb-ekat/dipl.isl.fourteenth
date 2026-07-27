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
from config import SEGMENTS_DIR, ENTITIES_DIR, MODEL, MAX_TOKENS

# ── System prompt (will be cached) ─────────────────────────────────────────
SYSTEM_PROMPT = """You are an expert in medieval Icelandic documentary history specialising in the Diplomatarium Islandicum (DI) — the published edition of Icelandic medieval charters (bréf) and related documents from the 9th to 16th centuries.

CRITICAL — CLOSED-BOOK EXTRACTION: Base every field strictly on the segment text given below, not on outside knowledge of the Diplomatarium Islandicum or any specific document in it, even if you recognize it. The segment's editorial apparatus may mention or cross-reference other documents (e.g. "sjá Nr. 21", a footnote citing a related privilege, a fascicle grouping) without quoting them — in that case, extract only what THIS segment's own text states, and do not describe the content, persons, dates, scribes, or seals of those other referenced documents from memory. If di_reference names a range (e.g. "nr. 4–7"), only report the range as such if the full text of each numbered document actually appears in the segment below; otherwise extract the single document this segment actually contains.

Your task is to extract structured data from charter transcriptions. Each charter may be in Old/Middle Icelandic, Medieval Latin, or a mixture. Apply the following rules:

PERSONS
- Extract every named individual who is a genuine period-contemporary actor in the document itself: someone who issued, received, witnessed, sealed, notarized, or otherwise participated in the ORIGINAL transaction at the time it was made.
- Use these role_category values (extend with similar dot-notation if needed):
    issuer-priest, issuer-layman, issuer-bishop, issuer-lawman,
    recipient, principal-opponent,
    witness-testimony, witness-boundary, witness-sealing-priest, witness-sealing-layman,
    scribe, notary, saint-patron
- Record patronymics as part of the name (e.g. "Jón Koðrason", not "Jón" + "Koðrason").
- For clergy, include their see or title as a qualifier if stated (e.g. qualifier: "Bishop of Hólar").
- CRITICAL — do NOT add a persons entry for anyone described only in terms of the document's LATER transmission history or DI's own modern editorial apparatus: a scribe/copyist who transcribed a SURVIVING COPY at a later date (even if self-attested in the text, e.g. "Árni Magnússon's scribe, attested 1712" for a document dated centuries earlier), a modern editor or scholar referenced in the apparatus (e.g. "Vilhjálmur Finsen, editor of Grágás"), or someone who "discovered" the manuscript or added marginal annotations centuries later. These are real facts worth preserving, but NOT persons of the charter — a person record's floruit is derived from the charter's own date, so putting a later actor there produces a self-contradictory record (e.g. floruit 1150 for someone "attested 1712"). Record this information in scribe_source instead (free text — append with "; " if it already holds the original scribe's identification), never in persons.

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
    """Load all previously-saved charter rows (successes AND error placeholders),
    keyed by filename. Callers decide what counts as 'done' via is_error_result."""
    if not out_path.exists():
        return {}
    with open(out_path, encoding="utf-8") as f:
        data = json.load(f)
    return {item["filename"]: item for item in data}


def is_error_result(item: dict) -> bool:
    """True if a previously-saved row represents a failed call that should be
    retried on the next run, rather than a permanent success."""
    return "_parse_error" in item or "_api_error" in item


def save_results(results: list[dict], out_path: Path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)


class CharterParseError(Exception):
    """Claude's response wasn't valid JSON. Carries the raw text so a failed
    response can be inspected/salvaged rather than only its exception message."""
    def __init__(self, message: str, raw_text: str):
        super().__init__(message)
        self.raw_text = raw_text


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
    candidate = raw
    if candidate.startswith("```"):
        candidate = candidate.split("```")[1]
        if candidate.startswith("json"):
            candidate = candidate[4:]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as e:
        raise CharterParseError(str(e), raw_text=raw) from e


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

    # Filter to requested range. Charters that previously failed (_parse_error
    # or _api_error) are retried automatically on a plain re-run, not
    # permanently skipped — a re-run's --start/--end already scopes which
    # charters are touched, so no separate opt-in flag is needed.
    charters_to_process = [
        row for row in index
        if args.start <= int(row["sequence"]) <= (args.end or len(index))
        and (row["filename"] not in existing or is_error_result(existing[row["filename"]]))
    ]
    retry_filenames = {row["filename"] for row in charters_to_process}
    # Keep prior rows we are NOT about to reprocess (successes, or errors
    # outside this run's --start/--end range).
    results = [item for fn, item in existing.items() if fn not in retry_filenames]

    n_retrying = sum(1 for fn in retry_filenames if fn in existing)
    print(f"[vol {args.vol}] {len(charters_to_process)} charters to process "
          f"({len(existing) - n_retrying} already done, "
          f"{n_retrying} previously failed and will retry).")

    for i, row in enumerate(charters_to_process, start=1):
        txt_path = vol_dir / row["filename"]
        if not txt_path.exists():
            print(f"  [{i}/{len(charters_to_process)}] SKIP (file missing): {row['filename']}")
            continue

        charter_text = txt_path.read_text(encoding="utf-8")
        print(f"  [{i}/{len(charters_to_process)}] Processing {row['filename']} …", end=" ", flush=True)

        try:
            extracted = extract_charter(client, charter_text)
        except CharterParseError as e:
            print(f"PARSE ERROR — {e}")
            extracted = {"_parse_error": str(e), "_raw_response": e.raw_text}
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

        # Save after every charter so a kill mid-run only loses the charter
        # currently in flight, not up to BATCH_SIZE-1 already-completed ones.
        save_results(results, out_path)

    print(f"\nDone. Results in {out_path}")


if __name__ == "__main__":
    main()
