"""
Step 1: Extract and segment charter text from DI PDF volumes.

Usage:
    python 01_extract_text.py --pdf path/to/Bindi_1.pdf [--vol 1]
    python 01_extract_text.py --pdf-dir ~/Downloads --pattern "Bindi_*.pdf"

Outputs (in output/segments/vol{N}/):
    DI_{vol}_{seq:04d}.txt   — one file per charter segment
    charter_index.csv        — maps each file → volume, sequence, page_start, raw_date_header
"""

import argparse
import csv
import re
import subprocess
import sys
from pathlib import Path

# Add parent dir to path so config is importable whether run from root or pipeline/
sys.path.insert(0, str(Path(__file__).parent))
from config import SEGMENTS_DIR, OUTPUT_DIR

# ── Date header patterns ────────────────────────────────────────────────────
# DI volumes use several header styles across different periods/editors:
#   "15. Mai 834."          day. MonthName Year.
#   "1341."                 year only
#   "1341. Júní 14."        year. MonthName day.
#   "1341, Júní 14."        year, MonthName day.  (some volumes use comma)
_HEADER_PATTERNS = [
    # {seq}. {whitespace} MonthName Year.  (e.g. "3.     April 846." — DI layout format)
    re.compile(r"^\s*\d{1,3}\.\s{2,}[A-Za-zÀ-öø-ÿÞþÐðÆæÖö]{3,}\.?\s+(?:8[3-9]\d|9\d\d|1[0-5]\d\d)\.?\s*$"),
    # Year. MonthName day.  (e.g. "1341. Júní 14.")
    re.compile(r"^\s*(?:8[3-9]\d|9\d\d|1[0-5]\d\d)[.,]\s+[A-Za-zÀ-öø-ÿÞþÐðÆæÖö]{3,}\.?\s+\d{1,2}\.?\s*$"),
    # Year only — constrained to DI date range 834-1599
    re.compile(r"^\s*(?:8[3-9]\d|9\d\d|1[0-5]\d\d)\.\s*$"),
    # Year range  (e.g. "1341—1345.")
    re.compile(r"^\s*(?:8[3-9]\d|9\d\d|1[0-5]\d\d)[—–-](?:8[3-9]\d|9\d\d|1[0-5]\d\d)\.?\s*$"),
]

FOOTNOTE_RE = re.compile(r"^\s*\d+\)\s")  # "1) footnote text"


def is_charter_header(line: str, allow_year_range: bool = True) -> bool:
    """allow_year_range=False disables pattern #4 (bare year range, e.g.
    "834—1264."). DI volume title pages state the whole volume's covering
    date range in exactly this shape, which false-positives as a charter
    header when no charter has opened yet — see segment_volume()."""
    for i, pattern in enumerate(_HEADER_PATTERNS):
        if i == 3 and not allow_year_range:
            continue
        if pattern.match(line):
            return True
    return False


def extract_text_from_pdf(pdf_path: Path) -> str:
    """Use pdftotext (poppler) to extract full text from a searchable PDF."""
    result = subprocess.run(
        ["pdftotext", "-layout", str(pdf_path), "-"],
        capture_output=True, text=True, encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError(f"pdftotext failed on {pdf_path}: {result.stderr[:300]}")
    return result.stdout


def extract_text_with_pages(pdf_path: Path) -> list[tuple[int, str]]:
    """Return list of (page_number, page_text) tuples."""
    result = subprocess.run(
        ["pdftotext", "-layout", "-f", "1", str(pdf_path), "-"],
        capture_output=True, text=True, encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError(f"pdftotext failed: {result.stderr[:300]}")

    pages = result.stdout.split("\x0c")  # form-feed separates pages
    return [(i + 1, page) for i, page in enumerate(pages)]


def segment_volume(pages: list[tuple[int, str]]) -> list[dict]:
    """
    Split a volume's text into individual charter blocks.
    Returns list of dicts: {seq, page_start, date_header, text}
    """
    charters = []
    current: dict | None = None

    for page_num, page_text in pages:
        lines = page_text.splitlines()
        for line in lines:
            stripped = line.strip()
            if not stripped:
                if current:
                    current["text"] += "\n"
                continue

            if current is None and _HEADER_PATTERNS[3].match(stripped):
                print(f"[vol] Skipping suspected front-matter year-range line "
                      f"(page {page_num}): {stripped!r} — not opening charter #1 with it.")

            if is_charter_header(stripped, allow_year_range=current is not None):
                # Save previous charter
                if current:
                    charters.append(current)
                current = {
                    "seq": len(charters) + 1,
                    "page_start": page_num,
                    "date_header": stripped,
                    "text": stripped + "\n",
                }
            elif current is not None:
                # Skip footnote lines
                if FOOTNOTE_RE.match(line):
                    continue
                current["text"] += line + "\n"

    if current:
        charters.append(current)

    return charters


def warn_on_length_outliers(charters: list[dict], vol_num: int, multiplier: float = 6.0) -> None:
    """Print a loud warning if any segment is a suspicious multiple of the volume's
    median segment length — a strong signal of front-matter contamination or a
    missed header, so a human notices at Step 1 instead of 3 steps later."""
    if len(charters) < 3:
        return
    lengths = sorted(len(c["text"].splitlines()) for c in charters)
    median = lengths[len(lengths) // 2] or 1
    for c in charters:
        length = len(c["text"].splitlines())
        if length > multiplier * median:
            print(
                f"[vol {vol_num}] WARNING: charter seq {c['seq']} is {length} lines "
                f"({length / median:.1f}x the volume median of {median}) — likely "
                f"front-matter contamination or mis-segmentation. Inspect "
                f"DI_{vol_num:02d}_{c['seq']:04d}.txt manually before running "
                f"02_extract_entities.py on this volume."
            )


def infer_volume_number(pdf_path: Path) -> int:
    """Best-effort: DI PDFs are named like 'Diplomatarium_Islandicum___Bindi_14.pdf' —
    use the trailing integer in the filename as the volume number, rather than
    positional/lexicographic ordering (which sorts Bindi_10 before Bindi_2)."""
    m = re.search(r"(\d+)\s*$", pdf_path.stem)
    if not m:
        raise ValueError(f"Could not infer DI volume number from filename: {pdf_path.name}")
    return int(m.group(1))


def process_volume(pdf_path: Path, vol_num: int) -> Path:
    """Extract, segment, and save one DI volume. Returns path to charter_index.csv."""
    print(f"[vol {vol_num}] Extracting text from {pdf_path.name} …")
    pages = extract_text_with_pages(pdf_path)
    print(f"[vol {vol_num}] {len(pages)} pages extracted.")

    charters = segment_volume(pages)
    print(f"[vol {vol_num}] {len(charters)} charter segments found.")
    warn_on_length_outliers(charters, vol_num)

    vol_dir = SEGMENTS_DIR / f"vol{vol_num:02d}"
    vol_dir.mkdir(parents=True, exist_ok=True)

    index_rows = []
    for ch in charters:
        filename = f"DI_{vol_num:02d}_{ch['seq']:04d}.txt"
        out_path = vol_dir / filename
        out_path.write_text(ch["text"], encoding="utf-8")
        index_rows.append({
            "filename": filename,
            "volume": vol_num,
            "sequence": ch["seq"],
            "page_start": ch["page_start"],
            "date_header": ch["date_header"],
        })

    index_path = vol_dir / "charter_index.csv"
    with open(index_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["filename", "volume", "sequence", "page_start", "date_header"])
        writer.writeheader()
        writer.writerows(index_rows)

    print(f"[vol {vol_num}] Saved to {vol_dir}  |  Index: {index_path.name}")
    return index_path


def main():
    parser = argparse.ArgumentParser(description="Extract charter segments from DI PDF volumes.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pdf", type=Path, help="Path to a single PDF volume.")
    group.add_argument("--pdf-dir", type=Path, help="Directory containing multiple PDF volumes.")
    parser.add_argument("--vol", type=int, default=1, help="Volume number (used with --pdf). Default: 1.")
    parser.add_argument("--pattern", default="Diplomatarium_Islandicum___Bindi_*.pdf",
                        help="Glob pattern for PDFs when using --pdf-dir.")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    SEGMENTS_DIR.mkdir(parents=True, exist_ok=True)

    if args.pdf:
        process_volume(args.pdf, args.vol)
    else:
        pdfs = sorted(args.pdf_dir.glob(args.pattern), key=infer_volume_number)
        if not pdfs:
            print(f"No PDFs found matching {args.pattern} in {args.pdf_dir}", file=sys.stderr)
            sys.exit(1)
        for pdf in pdfs:
            process_volume(pdf, infer_volume_number(pdf))

    print("Done. Check output/segments/ for charter text files.")


if __name__ == "__main__":
    main()
