"""
Central configuration for the Diplomatarium Islandicum extraction pipeline.
Edit paths and thresholds here before running any step.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ── Paths ──────────────────────────────────────────────────────────────────
# Directory containing your DI PDF volumes (e.g. Diplomatarium_Islandicum___Bindi_1.pdf)
# Override via PDF_DIR in .env — these locations move when machines/accounts get
# reorganized, so don't rely on the hardcoded default surviving.
PDF_DIR = Path(os.environ.get("PDF_DIR", Path.home() / "Desktop" / "Charters" / "pdfs"))

# Output root — intermediate files and final CSVs land here
OUTPUT_DIR = Path(__file__).parent / "output"
SEGMENTS_DIR = OUTPUT_DIR / "segments"   # one .txt per charter
ENTITIES_DIR = OUTPUT_DIR / "entities"  # raw JSON from Claude API
REVIEW_DIR   = OUTPUT_DIR / "review"    # per-volume CSVs for manual review

# Authority file (read-only during extraction; updated only by 06_merge_into_xlsx.py)
# Override via AUTHORITY_FILE in .env — see note on PDF_DIR above.
AUTHORITY_FILE = Path(os.environ.get(
    "AUTHORITY_FILE", Path.home() / "Desktop" / "McGill" / "diss" / "CHARTER_authority_file.xlsx"
))

# nafnid.is (Árnastofnun) place-name reconciliation — supplementary geocoding
# source alongside the Wikidata lookup in 04_lookup_coords.py. See
# nafnid/README.md for the pull-script usage caveat.
NAFNID_DIR         = Path(__file__).parent / "nafnid"
NAFNID_DATA_DIR    = NAFNID_DIR / "data"
NAFNID_LOOKUP_DIR  = NAFNID_DIR / "lookup_tables"

# ── Claude API ─────────────────────────────────────────────────────────────
MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 4096
BATCH_SIZE = 10          # charters per API call (each is a separate message)

# ── Entity resolution thresholds ───────────────────────────────────────────
FUZZY_ACCEPT  = 85   # score ≥ this → auto-assign existing ID
FUZZY_REVIEW  = 60   # score in [60,85) → flag for manual review
                     # score < 60 → treat as new entity

# When resolve_places() is about to mint a brand-new place_id, first check it
# against the OTHER new places already minted earlier in the SAME charter
# (not the full authority) to catch near-duplicate spellings of one place
# (e.g. "Hamaburg" vs "Hammaburg"). Measured against real examples, genuine
# spelling variants of the same place scored 87-94 (token_sort_ratio), while
# distinct-but-similar short Icelandic place-name elements (e.g.
# "Fell"/"Felli", "Nes"/"Nesi") scored 83-89 -- there is no threshold that
# cleanly separates the two, so this is deliberately close to (not far above)
# FUZZY_ACCEPT rather than "stricter", combined with variant accumulation
# in resolve_places() so repeated near-duplicate mentions still converge.
NEW_PLACE_DEDUP_THRESHOLD = 88

# ── Segmentation ───────────────────────────────────────────────────────────
# Regex for DI charter date headers (covers most volume formats)
# Matches: "15. Mai 834." or "1341." or "1341. júní 14."
import re
CHARTER_HEADER_RE = re.compile(
    r"^\s*(\d{1,2}\.\s+\w+\s+\d{3,4}|\d{3,4}\.\s+\w+\s+\d{1,2}|\d{3,4})\.\s*$",
    re.MULTILINE,
)
