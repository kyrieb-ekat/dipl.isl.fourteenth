"""
Step 1b: Apply high-confidence, auto-fixable OCR corrections to charter
segments, writing the result to a SEPARATE tree (output/segments_corrected/)
rather than touching output/segments/ in place.

Why a separate tree, not in-place editing: vol01 and vol04 have already been
fully processed by 02_extract_entities.py -> 03_resolve_entities.py -> the
live DB, including real human review decisions. This codebase has no
hash/mtime/content-fingerprint mechanism anywhere linking a DB row back to
the segment file it was extracted from -- only (volume, sequence). Editing
output/segments/ in place for an already-processed volume would risk the
same silent-staleness failure class already hit once before
(02_extract_entities.py's resume-by-filename cache going stale across a
segmentation change, producing a real duplicate charter in the live DB).
Writing corrected text to a new, separate location instead makes that class
of bug structurally impossible here.

Fixes ONLY the highest-confidence corruption pattern: a small set of
2-character bracket/pipe/angle-glyph clusters that DI's source OCR uses to
consistently misrender lowercase thorn (þ). Validated by hand against every
real occurrence in vol01+vol04 (633 total bracket_cluster matches): these 7
shapes account for 598 (94.5%) of them, and every single sampled instance of
each shape is unambiguously þ. Everything else -- rarer/messier bracket
shapes, the separate known ð->"fe" corruption, and the H-misread h_pattern
heuristic -- is deliberately NOT auto-fixed here; see 01c_flag_ocr_for_review.py,
which surfaces the rest for human review.

Usage:
    python 01b_apply_ocr_corrections.py                  # dry run, every vol* dir
    python 01b_apply_ocr_corrections.py --vol 1           # dry run, vol01 only
    python 01b_apply_ocr_corrections.py --vol 1 --confirm # write segments_corrected/vol01

Reads:  output/segments/vol{N}/charter_index.csv + DI_{N}_{seq}.txt (untouched)
Writes: output/segments_corrected/vol{N}/charter_index.csv + DI_{N}_{seq}.txt
        output/review/vol{N}_ocr_corrections_applied.csv (changelog)
"""

import argparse
import csv
import importlib.util
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import SEGMENTS_DIR, SEGMENTS_CORRECTED_DIR, REVIEW_DIR

_audit_spec = importlib.util.spec_from_file_location(
    "_audit_ocr_quality_01a", Path(__file__).parent / "01a_audit_ocr_quality.py"
)
_audit = importlib.util.module_from_spec(_audit_spec)
_audit_spec.loader.exec_module(_audit)

# Every occurrence of each shape sampled by hand across vol01+vol04 (633 total
# bracket_cluster matches); zero counter-examples found. Deliberately an
# ALLOW-list, not a deny-list -- a shape not in this dict is always left
# untouched, even one that superficially resembles a safe one ("<)" and "<>"
# were both sampled and found genuinely ambiguous -- ð in one instance, ö in
# another, unreadable garbage in a third -- and are intentionally excluded
# rather than guessed at).
SAFE_SHAPES: dict[str, str] = {
    "|)": "þ",   # 176 occurrences -- e.g. "|)ar" -> "þar"
    "])": "þ",   # 157            -- e.g. "])etta" -> "þetta"
    "|>": "þ",   # 151            -- e.g. "|>at" -> "þat"
    "]>": "þ",   #  70            -- e.g. "]>afe" -> "þafe"
    "[>": "þ",   #  28            -- e.g. "[>eim" -> "þeim"
    ")>": "þ",   #  10            -- e.g. ")>essu" -> "þessu"
    "[)": "þ",   #   6            -- e.g. "[)etta" -> "þetta"
}

EXCERPT_RADIUS = _audit.EXCERPT_RADIUS

CHANGELOG_FIELDNAMES = [
    "filename", "volume", "sequence", "date_header",
    "char_offset", "matched_text", "matched_shape", "corrected_char",
    "context_before", "context_after",
]


def classify_matches(text: str) -> tuple[list[dict], list[dict]]:
    """Every bracket_cluster match in `text`, split into (safe, not_safe).
    Offsets are into `text` (the original, uncorrected segment)."""
    safe, not_safe = [], []
    for m in _audit.THORN_BRACKET_RE.finditer(text):
        start, end = m.span()  # end == start + 3, always (2 bracket chars + 1 letter)
        shape = text[start:start + 2]
        record = {"start": start, "end": end, "matched_text": text[start:end], "shape": shape}
        if shape in SAFE_SHAPES:
            record["replacement"] = SAFE_SHAPES[shape]
            safe.append(record)
        else:
            not_safe.append(record)
    return safe, not_safe


def apply_corrections(text: str, safe_matches: list[dict]) -> str:
    """Rebuilds text left-to-right. Each correction consumes the 2-char
    bracket shape and emits 1 replacement char; the 3rd matched char (the
    already-correct lowercase letter) is left untouched by advancing the
    cursor only 2 past match start, so the following slice picks it up
    verbatim -- this is what turns "])etta" into "þetta", not "þtta"."""
    if not safe_matches:
        return text
    parts, cursor = [], 0
    for c in safe_matches:  # already left-to-right, non-overlapping (re.finditer guarantee)
        parts.append(text[cursor:c["start"]])
        parts.append(c["replacement"])
        cursor = c["start"] + 2
    parts.append(text[cursor:])
    return "".join(parts)


def _context(text: str, start: int, end: int) -> tuple[str, str]:
    lo = max(0, start - EXCERPT_RADIUS)
    hi = min(len(text), end + EXCERPT_RADIUS)
    return (" ".join(text[lo:start].split()), " ".join(text[end:hi].split()))


def process_volume(vol_num: int, confirm: bool) -> dict:
    vol_dir = SEGMENTS_DIR / f"vol{vol_num:02d}"
    out_dir = SEGMENTS_CORRECTED_DIR / f"vol{vol_num:02d}"
    index_rows = _audit.load_index(vol_dir)

    changelog_rows = []
    n_files_with_fix = 0
    n_safe_total = 0
    n_not_safe_total = 0

    if confirm:
        out_dir.mkdir(parents=True, exist_ok=True)

    for entry in index_rows:
        txt_path = vol_dir / entry["filename"]
        text = txt_path.read_text(encoding="utf-8")
        safe, not_safe = classify_matches(text)
        n_safe_total += len(safe)
        n_not_safe_total += len(not_safe)

        corrected = apply_corrections(text, safe) if safe else text
        if safe:
            n_files_with_fix += 1

        for c in safe:
            before, after = _context(text, c["start"], c["end"])
            changelog_rows.append({
                "filename": entry["filename"], "volume": vol_num, "sequence": entry["sequence"],
                "date_header": entry["date_header"], "char_offset": c["start"],
                "matched_text": c["matched_text"], "matched_shape": c["shape"],
                "corrected_char": c["replacement"],
                "context_before": before, "context_after": after,
            })

        if confirm:
            (out_dir / entry["filename"]).write_text(corrected, encoding="utf-8")

    if confirm:
        shutil.copy2(vol_dir / "charter_index.csv", out_dir / "charter_index.csv")

        REVIEW_DIR.mkdir(parents=True, exist_ok=True)
        changelog_path = REVIEW_DIR / f"vol{vol_num:02d}_ocr_corrections_applied.csv"
        with open(changelog_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CHANGELOG_FIELDNAMES)
            writer.writeheader()
            writer.writerows(changelog_rows)

    return {
        "vol_num": vol_num, "n_segments": len(index_rows),
        "n_files_with_fix": n_files_with_fix,
        "n_safe_total": n_safe_total, "n_not_safe_total": n_not_safe_total,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Auto-fix the highest-confidence OCR corruption pattern "
                    "(bracket-cluster thorn misreads) into a separate output tree, "
                    "never touching output/segments/ in place."
    )
    parser.add_argument("--vol", type=int, default=None,
                        help="Volume number to process. Default: every vol* dir under output/segments/.")
    parser.add_argument("--confirm", action="store_true",
                        help="Actually write segments_corrected/ + the changelog CSV. "
                             "Without this flag, only a dry-run summary is printed.")
    args = parser.parse_args()

    if args.vol is not None:
        vol_nums = [args.vol]
    else:
        vol_nums = sorted(
            int(p.name.replace("vol", "")) for p in SEGMENTS_DIR.glob("vol*") if p.is_dir()
        )

    if not vol_nums:
        print(f"No vol* directories found under {SEGMENTS_DIR}", file=sys.stderr)
        sys.exit(1)

    for vol_num in vol_nums:
        vol_dir = SEGMENTS_DIR / f"vol{vol_num:02d}"
        if not (vol_dir / "charter_index.csv").exists():
            print(f"[vol {vol_num}] SKIP: no charter_index.csv in {vol_dir}", file=sys.stderr)
            continue

        stats = process_volume(vol_num, args.confirm)
        mode = "APPLIED" if args.confirm else "DRY RUN"
        print(f"[vol {vol_num}] {mode}: {stats['n_segments']} segments scanned, "
              f"{stats['n_files_with_fix']} file(s) with >=1 safe fix, "
              f"{stats['n_safe_total']} safe correction(s), "
              f"{stats['n_not_safe_total']} not-safe match(es) left for review")
        if args.confirm:
            out_dir = SEGMENTS_CORRECTED_DIR / f"vol{vol_num:02d}"
            changelog_path = REVIEW_DIR / f"vol{vol_num:02d}_ocr_corrections_applied.csv"
            print(f"[vol {vol_num}] Corrected segments -> {out_dir}")
            print(f"[vol {vol_num}] Changelog -> {changelog_path}")

    if not args.confirm:
        print("\nDry run only. Re-run with --confirm to write segments_corrected/ and the changelog CSV.")


if __name__ == "__main__":
    main()
