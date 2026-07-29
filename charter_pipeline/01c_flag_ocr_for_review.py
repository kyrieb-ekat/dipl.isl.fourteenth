"""
Step 1c: Surface everything NOT auto-fixed by 01b_apply_ocr_corrections.py for
human review, via the review app's unified Review Queue (the "ocr_fix" item
type in review_queue.py).

Scans output/segments_corrected/ (01b's output, the corrected text) -- NOT
the live output/segments/ -- since whatever bracket_cluster matches remain
after 01b's fix are, by definition, the shapes not safe to auto-correct; no
separate whitelist/coordination with 01b is needed here. Also flags every
density_outlier segment and every h_pattern match from that same, final,
post-fix text.

Run this AFTER 01b_apply_ocr_corrections.py --confirm for a volume. Safe to
re-run: inserts are deduped against ocr_flags' existing rows (see
db.bulk_insert_ocr_flags), so re-running never creates duplicate flags.

Usage:
    python 01c_flag_ocr_for_review.py                  # every vol* dir under output/segments_corrected/
    python 01c_flag_ocr_for_review.py --vol 1

Reads:  output/segments_corrected/vol{N}/charter_index.csv + DI_{N}_{seq}.txt
Writes: ocr_flags rows in charter_pipeline.db
        output/review/vol{N}_ocr_quality_audit.csv (refreshed against corrected text)
"""

import argparse
import importlib.util
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import SEGMENTS_CORRECTED_DIR
import db

_audit_spec = importlib.util.spec_from_file_location(
    "_audit_ocr_quality_01a", Path(__file__).parent / "01a_audit_ocr_quality.py"
)
_audit = importlib.util.module_from_spec(_audit_spec)
_audit_spec.loader.exec_module(_audit)


def _bracket_cluster_rows(row: dict) -> list[dict]:
    """Every remaining bracket_cluster match -- by construction, only the
    shapes 01b_apply_ocr_corrections.py's SAFE_SHAPES didn't cover (anything
    it DID fix is no longer present in this, the post-fix text)."""
    text = row["_text"]
    out = []
    for start, end in row["_bracket_match_spans"]:
        lo = max(0, start - _audit.EXCERPT_RADIUS)
        hi = min(len(text), end + _audit.EXCERPT_RADIUS)
        shape = text[start:start + 2]
        out.append({
            "volume": row["volume"], "sequence": int(row["sequence"]), "heuristic": "bracket_cluster",
            "char_start": start, "char_end": end, "matched_text": text[start:end], "shape": shape,
            "excerpt": " ".join(text[lo:hi].split()), "excerpt_offset": lo,
            "page_start": row["page_start"], "segment_length": row["nonspace_chars"],
            "metric_value": None, "metric_reference": None,
            "detail": f"bracket_cluster: shape {shape!r} not in the auto-fix safe list -- "
                      f"needs a human reading against the source page",
            "suspicion_score": 2,
        })
    return out


def _h_pattern_rows(row: dict) -> list[dict]:
    text = row["_text"]
    out = []
    for start, end in row["_h_pattern_spans"]:
        lo = max(0, start - _audit.EXCERPT_RADIUS)
        hi = min(len(text), end + _audit.EXCERPT_RADIUS)
        out.append({
            "volume": row["volume"], "sequence": int(row["sequence"]), "heuristic": "h_pattern",
            "char_start": start, "char_end": end, "matched_text": text[start:end], "shape": "",
            "excerpt": " ".join(text[lo:hi].split()), "excerpt_offset": lo,
            "page_start": row["page_start"], "segment_length": row["nonspace_chars"],
            "metric_value": None, "metric_reference": None,
            "detail": "h_pattern: possible capital-H misread as \"Il\"/\"ll\" -- confirmed "
                      "false-positive family: \"Illugi\"/\"Illuga\" is a real name, verify "
                      "against source before correcting",
            "suspicion_score": 1,
        })
    return out


def _density_outlier_row(row: dict, volume_median: float) -> dict:
    text = row["_text"]
    newline_idx = text.find("\n")
    body_offset = newline_idx + 1 if newline_idx != -1 else 0
    excerpt = " ".join(text[body_offset:body_offset + 150].split())
    density = row["thorn_eth_density_per_1000"]
    percentile = row["density_percentile_in_volume"]
    return {
        "volume": row["volume"], "sequence": int(row["sequence"]), "heuristic": "density_outlier",
        "char_start": None, "char_end": None, "matched_text": "", "shape": "",
        "excerpt": excerpt, "excerpt_offset": body_offset,
        "page_start": row["page_start"], "segment_length": row["nonspace_chars"],
        "metric_value": density, "metric_reference": volume_median,
        "detail": f"density_outlier: {density:.1f} þ/ð per 1000 chars "
                  f"(volume median {volume_median:.1f}, {percentile:.0f}th percentile) -- "
                  f"may be genuine Latin content or a terse inventory/máldagi entry, "
                  f"verify against source",
        "suspicion_score": 5,
    }


def flag_volume(vol_num: int, min_chars: int, percentile: float) -> dict:
    vol_dir = SEGMENTS_CORRECTED_DIR / f"vol{vol_num:02d}"
    rows = _audit.audit_volume(vol_num, vol_dir, min_chars, percentile)

    assessable_densities = [r["thorn_eth_density_per_1000"] for r in rows if not r["too_short_to_assess"]]
    volume_median = statistics.median(assessable_densities) if assessable_densities else 0.0

    candidates = []
    for row in rows:
        candidates.extend(_bracket_cluster_rows(row))
        candidates.extend(_h_pattern_rows(row))
        if row["density_outlier_flag"]:
            candidates.append(_density_outlier_row(row, volume_median))

    inserted = db.bulk_insert_ocr_flags(candidates)

    # Also refresh the dashboard audit CSV against the corrected text, so
    # output/review/vol{N}_ocr_quality_audit.csv reflects post-fix reality
    # rather than the pre-fix numbers from the original 01a run.
    _audit.write_report(rows, vol_num)

    return {
        "vol_num": vol_num, "n_segments": len(rows), "n_candidates": len(candidates),
        "n_inserted": inserted,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Flag everything not auto-fixed by 01b_apply_ocr_corrections.py "
                    "into the ocr_flags table for review in review_app.py's Review Queue."
    )
    parser.add_argument("--vol", type=int, default=None,
                        help="Volume number. Default: every vol* dir under output/segments_corrected/.")
    parser.add_argument("--min-chars", type=int, default=300)
    parser.add_argument("--percentile", type=float, default=10)
    args = parser.parse_args()

    if args.vol is not None:
        vol_nums = [args.vol]
    else:
        vol_nums = sorted(
            int(p.name.replace("vol", "")) for p in SEGMENTS_CORRECTED_DIR.glob("vol*") if p.is_dir()
        )

    if not vol_nums:
        print(f"No vol* directories found under {SEGMENTS_CORRECTED_DIR} -- "
              f"run 01b_apply_ocr_corrections.py --confirm first.", file=sys.stderr)
        sys.exit(1)

    for vol_num in vol_nums:
        vol_dir = SEGMENTS_CORRECTED_DIR / f"vol{vol_num:02d}"
        if not (vol_dir / "charter_index.csv").exists():
            print(f"[vol {vol_num}] SKIP: no charter_index.csv in {vol_dir} -- "
                  f"run 01b_apply_ocr_corrections.py --confirm first.", file=sys.stderr)
            continue
        stats = flag_volume(vol_num, args.min_chars, args.percentile)
        print(f"[vol {vol_num}] {stats['n_segments']} segments scanned, "
              f"{stats['n_candidates']} candidate flag(s) found, "
              f"{stats['n_inserted']} new row(s) inserted into ocr_flags "
              f"({stats['n_candidates'] - stats['n_inserted']} already present)")


if __name__ == "__main__":
    main()
