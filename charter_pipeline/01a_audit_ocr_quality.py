"""
Step 1a: Audit existing charter segments for likely OCR corruption of Icelandic
þ/ð/H, specifically the systematic thorn/eth/H glyph confusions confirmed in
DI's pre-existing OCR text layer (NOT produced by this pipeline -- pdftotext
just reads the PDF's own baked-in OCR layer; no OCR is performed here).

Read-only research tool: detects and reports, never modifies or corrects
anything, and is not wired into any other pipeline step.

Usage:
    python 01a_audit_ocr_quality.py                  # every vol* dir under output/segments/
    python 01a_audit_ocr_quality.py --vol 1
    python 01a_audit_ocr_quality.py --top-n 20 --min-chars 300 --percentile 10

Reads:  output/segments/vol{N}/charter_index.csv + DI_{N}_{seq}.txt
Writes: output/review/vol{N}_ocr_quality_audit.csv
"""

import argparse
import csv
import re
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import SEGMENTS_DIR, REVIEW_DIR

# ── Heuristic: bracket_cluster (high confidence) ────────────────────────────
# DI's OCR confuses þ/ð/other special glyphs for clusters of bracket/pipe/angle
# characters (confirmed real examples: "])etta" for "þetta", "al])íng" for
# "alþíng", "verife" for "verið"). Two ADJACENT, DIFFERENT bracket-family
# characters followed by a lowercase letter distinguishes this from two real,
# legitimate editorial conventions found in this edition: a single
# editorially-supplied letter inside matching brackets ("Kví(g)andafells",
# "Ret[t]" -- always has a letter BETWEEN the brackets, never two bracket
# chars touching) and doubled-identical-paren quotation marking direct
# manuscript/colophon text ("((þa er Magnus...", "...endaz bok her))" --
# always the SAME character repeated, never two different ones). Validated
# by hand against ~100 real matches across vol01/vol04: no clear false
# positives. Known gap: requiring two adjacent bracket-family characters is
# what gives this its near-zero false-positive rate, so a corruption pattern
# that renders as only a single stray bracket character isn't caught here.
_BRACKET_CHARS = r"\[\]\(\)\|<>"
_LOWER = "a-záðéíóúýþæö"
THORN_BRACKET_RE = re.compile(rf"([{_BRACKET_CHARS}])(?!\1)[{_BRACKET_CHARS}][{_LOWER}]")

# ── Heuristic: h_pattern (supplementary, lower confidence only) ─────────────
# DI's OCR frequently misreads a capital H in running text as "Il"/"ll"/"I"
# (confirmed: "Ilann"->Hann, "Ilelgi"->Helgi, "llammaburgensi"->Hammaburgensi).
# NOT gated into the main suspicion flag: "Illugi"/"Illuga" is a real,
# fairly common medieval Icelandic personal/place name, indistinguishable
# from corruption by this pattern alone -- reported only as a supplementary
# count for a human to weigh alongside the other columns.
H_PATTERN_RE = re.compile(rf"\b[Il]{{2}}[{_LOWER}]")

DEFAULT_MIN_ASSESSABLE_CHARS = 300
DEFAULT_DENSITY_PERCENTILE = 10
EXCERPT_RADIUS = 60  # chars of context on each side of a bracket-cluster match

FIELDNAMES = [
    "filename", "volume", "sequence", "date_header", "nonspace_chars",
    "too_short_to_assess", "bracket_cluster_matches", "thorn_count", "eth_count",
    "thorn_eth_density_per_1000", "density_percentile_in_volume", "density_outlier_flag",
    "h_pattern_matches", "suspicion_score", "worst_match_excerpt",
]


def load_index(vol_dir: Path) -> list[dict]:
    """Same shape as 02_extract_entities.py's load_index() -- reimplemented
    locally since it's a small, stateless helper, to avoid a fragile
    cross-script import."""
    index_path = vol_dir / "charter_index.csv"
    with open(index_path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _nonspace_len(text: str) -> int:
    return len(re.sub(r"\s", "", text))


def analyze_segment(text: str, min_chars: int) -> dict:
    """Per-segment raw counts. Percentile/outlier flagging happens afterward,
    in compute_density_outliers(), once every segment in a volume has been
    analyzed once (density is only meaningful relative to its own volume)."""
    nonspace_chars = _nonspace_len(text)
    thorn_count = text.count("þ") + text.count("Þ")
    eth_count = text.count("ð") + text.count("Ð")
    bracket_matches = list(THORN_BRACKET_RE.finditer(text))
    h_matches = list(H_PATTERN_RE.finditer(text))

    too_short = nonspace_chars < min_chars
    density = (
        None if too_short or nonspace_chars == 0
        else (thorn_count + eth_count) / nonspace_chars * 1000
    )

    return {
        "nonspace_chars": nonspace_chars,
        "too_short_to_assess": too_short,
        "thorn_count": thorn_count,
        "eth_count": eth_count,
        "thorn_eth_density_per_1000": density,
        "bracket_cluster_matches": len(bracket_matches),
        "h_pattern_matches": len(h_matches),
        "_text": text,
        "_bracket_match_spans": [m.span() for m in bracket_matches],
        "_h_pattern_spans": [m.span() for m in h_matches],
    }


def compute_density_outliers(rows: list[dict], percentile: float) -> None:
    """Fills density_percentile_in_volume / density_outlier_flag in place,
    ranked against other ASSESSABLE segments in the SAME volume only --
    density is legitimately volume-dependent (a volume with more Latin
    content or more terse máldagi entries has a lower baseline), so
    comparing across volumes would measure genre mix, not OCR quality."""
    assessable = [r for r in rows if not r["too_short_to_assess"]]
    densities = sorted(r["thorn_eth_density_per_1000"] for r in assessable)

    cutoff = None
    if len(densities) >= 2:
        idx = max(0, min(98, round(percentile) - 1))
        cutoff = statistics.quantiles(densities, n=100, method="inclusive")[idx]

    for r in rows:
        if r["too_short_to_assess"]:
            r["density_percentile_in_volume"] = None
            r["density_outlier_flag"] = False
            continue
        rank_pct = 100.0 * sum(1 for d in densities if d <= r["thorn_eth_density_per_1000"]) / len(densities)
        r["density_percentile_in_volume"] = round(rank_pct, 1)
        r["density_outlier_flag"] = cutoff is not None and r["thorn_eth_density_per_1000"] <= cutoff


def _excerpt(row: dict) -> str:
    """~120 chars around the first bracket-cluster match if any exist;
    otherwise the segment body (skipping the date-header first line) if
    density-flagged, so a human can tell "this is Latin"/"this is a terse
    inventory entry" from "this is garbled Icelandic" in a few seconds;
    otherwise blank."""
    text = row["_text"]
    spans = row["_bracket_match_spans"]
    if spans:
        start, end = spans[0]
        lo = max(0, start - EXCERPT_RADIUS)
        hi = min(len(text), end + EXCERPT_RADIUS)
        return " ".join(text[lo:hi].split())
    if row["density_outlier_flag"]:
        body = text.split("\n", 1)[1] if "\n" in text else text
        return " ".join(body[:150].split())
    return ""


def audit_volume(vol_num: int, vol_dir: Path, min_chars: int, percentile: float) -> list[dict]:
    index_rows = load_index(vol_dir)
    rows = []
    for entry in index_rows:
        txt_path = vol_dir / entry["filename"]
        text = txt_path.read_text(encoding="utf-8")
        analysis = analyze_segment(text, min_chars)
        analysis.update({
            "filename": entry["filename"],
            "volume": vol_num,
            "sequence": entry["sequence"],
            "date_header": entry["date_header"],
            "page_start": int(entry["page_start"]),
        })
        rows.append(analysis)

    compute_density_outliers(rows, percentile)

    for r in rows:
        r["suspicion_score"] = (
            2 * r["bracket_cluster_matches"]
            + (5 if r["density_outlier_flag"] else 0)
            + r["h_pattern_matches"]
        )
        r["worst_match_excerpt"] = _excerpt(r)

    return rows


def write_report(rows: list[dict], vol_num: int) -> Path:
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REVIEW_DIR / f"vol{vol_num:02d}_ocr_quality_audit.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda r: -r["suspicion_score"]))
    return out_path


def print_summary(rows: list[dict], vol_num: int, top_n: int, percentile: float) -> None:
    n = len(rows)
    if n == 0:
        print(f"[vol {vol_num}] No segments found.")
        return

    bracket_hits = sum(1 for r in rows if r["bracket_cluster_matches"] > 0)
    bracket_total = sum(r["bracket_cluster_matches"] for r in rows)
    total_nonspace = sum(r["nonspace_chars"] for r in rows) or 1
    assessable = sum(1 for r in rows if not r["too_short_to_assess"])
    density_flagged = sum(1 for r in rows if r["density_outlier_flag"])
    h_hits = sum(1 for r in rows if r["h_pattern_matches"] > 0)

    print(f"[vol {vol_num}] Scanning {n} segments...")
    print(f"[vol {vol_num}] Bracket-cluster (thorn) matches: {bracket_hits}/{n} segments "
          f"({100 * bracket_hits / n:.1f}%), {bracket_total} instances, "
          f"{1000 * bracket_total / total_nonspace:.3f}/1000 chars")
    print(f"[vol {vol_num}] Density outliers (bottom {percentile:g}% thorn+eth density, "
          f"{assessable} assessable): {density_flagged}/{n} ({100 * density_flagged / n:.1f}%)")
    print(f"[vol {vol_num}] H-pattern matches (LOWER CONFIDENCE -- e.g. \"Illugi\" is a real "
          f"name, not corruption): {h_hits}/{n} ({100 * h_hits / n:.1f}%)")

    any_flag = sum(1 for r in rows if r["suspicion_score"] > 0)
    print(f"[vol {vol_num}] Segments flagged by >=1 heuristic: {any_flag}/{n} "
          f"({100 * any_flag / n:.1f}%)")

    worst = [r for r in sorted(rows, key=lambda r: -r["suspicion_score"]) if r["suspicion_score"] > 0][:top_n]
    print(f"[vol {vol_num}] Worst {len(worst)} by suspicion score:")
    for r in worst:
        dens = (
            f"density={r['thorn_eth_density_per_1000']:.1f} (p{r['density_percentile_in_volume']:.0f})"
            if r["thorn_eth_density_per_1000"] is not None else "density=n/a"
        )
        print(f"    {r['filename']} (seq {r['sequence']}): score={r['suspicion_score']}, "
              f"{r['bracket_cluster_matches']} bracket matches, {dens} "
              f"-- \"{r['worst_match_excerpt'][:80]}\"")


def main():
    parser = argparse.ArgumentParser(
        description="Audit existing charter segments for likely OCR corruption "
                     "(read-only; writes a CSV report, never modifies segment text)."
    )
    parser.add_argument("--vol", type=int, default=None,
                        help="Volume number to audit. Default: every vol* dir under output/segments/.")
    parser.add_argument("--min-chars", type=int, default=DEFAULT_MIN_ASSESSABLE_CHARS,
                        help=f"Minimum non-whitespace chars for density assessment. "
                             f"Default: {DEFAULT_MIN_ASSESSABLE_CHARS}.")
    parser.add_argument("--percentile", type=float, default=DEFAULT_DENSITY_PERCENTILE,
                        help=f"Flag segments at/below this percentile of the volume's own "
                             f"thorn+eth density distribution. Default: {DEFAULT_DENSITY_PERCENTILE}.")
    parser.add_argument("--top-n", type=int, default=10,
                        help="How many worst-offender segments to print per volume. Default: 10.")
    args = parser.parse_args()

    if args.vol is not None:
        vol_dirs = [(args.vol, SEGMENTS_DIR / f"vol{args.vol:02d}")]
    else:
        vol_dirs = sorted(
            (int(p.name.replace("vol", "")), p)
            for p in SEGMENTS_DIR.glob("vol*") if p.is_dir()
        )

    if not vol_dirs:
        print(f"No vol* directories found under {SEGMENTS_DIR}", file=sys.stderr)
        sys.exit(1)

    all_rows = []
    for vol_num, vol_dir in vol_dirs:
        if not (vol_dir / "charter_index.csv").exists():
            print(f"[vol {vol_num}] SKIP: no charter_index.csv in {vol_dir}", file=sys.stderr)
            continue
        rows = audit_volume(vol_num, vol_dir, args.min_chars, args.percentile)
        out_path = write_report(rows, vol_num)
        print_summary(rows, vol_num, args.top_n, args.percentile)
        print(f"[vol {vol_num}] Report -> {out_path}")
        print()
        all_rows.extend(rows)

    if len(all_rows) > 0:
        volumes = sorted({r["volume"] for r in all_rows})
        print("=== SUMMARY ===")
        print(f"Total segments scanned: {len(all_rows)}")
        if len(volumes) > 1:
            for vol_num in volumes:
                vol_rows = [r for r in all_rows if r["volume"] == vol_num]
                total_nonspace = sum(r["nonspace_chars"] for r in vol_rows) or 1
                bracket_total = sum(r["bracket_cluster_matches"] for r in vol_rows)
                print(f"  vol{vol_num:02d}: {1000 * bracket_total / total_nonspace:.3f} "
                      f"bracket-cluster matches/1000 chars")


if __name__ == "__main__":
    main()
