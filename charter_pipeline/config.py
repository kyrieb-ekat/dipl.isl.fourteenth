"""
Central configuration for the Diplomatarium Islandicum extraction pipeline.
Edit paths and thresholds here before running any step.
"""
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────
# Directory containing your DI PDF volumes (e.g. Diplomatarium_Islandicum___Bindi_1.pdf)
PDF_DIR = Path.home() / "Desktop" / "Charters" / "pdfs"

# Output root — intermediate files and final CSVs land here
OUTPUT_DIR = Path(__file__).parent / "output"
SEGMENTS_DIR = OUTPUT_DIR / "segments"   # one .txt per charter
ENTITIES_DIR = OUTPUT_DIR / "entities"  # raw JSON from Claude API
REVIEW_DIR   = OUTPUT_DIR / "review"    # per-volume CSVs for manual review

# Authority file (read-only during extraction; updated only by 06_merge_into_xlsx.py)
AUTHORITY_FILE = Path.home() / "Desktop" / "McGill" / "diss" / "CHARTER_authority_file.xlsx"

# ── Claude API ─────────────────────────────────────────────────────────────
MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 4096
BATCH_SIZE = 10          # charters per API call (each is a separate message)

# ── Entity resolution thresholds ───────────────────────────────────────────
FUZZY_ACCEPT  = 85   # score ≥ this → auto-assign existing ID
FUZZY_REVIEW  = 60   # score in [60,85) → flag for manual review
                     # score < 60 → treat as new entity

# ── Segmentation ───────────────────────────────────────────────────────────
# Regex for DI charter date headers (covers most volume formats)
# Matches: "15. Mai 834." or "1341." or "1341. júní 14."
import re
CHARTER_HEADER_RE = re.compile(
    r"^\s*(\d{1,2}\.\s+\w+\s+\d{3,4}|\d{3,4}\.\s+\w+\s+\d{1,2}|\d{3,4})\.\s*$",
    re.MULTILINE,
)
