"""
Step 1: Extract and segment charter text from DI PDF volumes.

Usage:
    python 01_extract_text.py --pdf path/to/Bindi_1.pdf [--vol 1]
    python 01_extract_text.py --pdf-dir ~/Downloads --pattern "Bindi_*.pdf"

Outputs (in output/segments/vol{N}/):
    DI_{vol}_{seq:04d}.txt   — one file per charter segment
    charter_index.csv        — maps each file → volume, sequence, page_start, raw_date_header
    flagged_for_review.csv   — lines an LLM classification pass couldn't confidently
                               resolve, or where it suspects an OCR digit misread in
                               a bracketed/conjectural year (never auto-corrected)

Segmentation strategy:
    1. The first non-blank line of every PDF page is treated as a running
       header/page-footer and excluded, regardless of whether it happens to
       look date-shaped — DI running headers repeat page number/section
       title/year in several inconsistent orders, and this positional signal
       is far more reliable than any text-shape match (confirmed: this is
       what was fragmenting long multi-part documents like Vilchinsbók into
       bogus segments, since one date-pattern coincidentally also matches a
       running-header shape).
    2. A fixed family of regexes (_HEADER_PATTERNS_ALWAYS / _GATED) matches
       genuine DI charter-header shapes cataloged from real volumes: plain
       dates, bracketed conjectural/circa/season/post-quem years, and dates
       with trailing place-name text.
    3. Anything shaped like a header but not strictly matched (a permissive,
       deliberately over-triggering check) is queued and classified by
       Claude in small batches — this is the safety net for header shapes
       not yet seen in any of the 14 DI volumes, and for telling a genuine
       new charter apart from a roman-numeral sub-entry inside one large
       multi-part document (e.g. a máldagi/property survey), which is
       shape-identical to a real opener and needs contextual judgment.
"""

import argparse
import csv
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import anthropic

# Add parent dir to path so config is importable whether run from root or pipeline/
sys.path.insert(0, str(Path(__file__).parent))
from config import SEGMENTS_DIR, OUTPUT_DIR, MODEL, MAX_TOKENS

# ── Date header patterns ────────────────────────────────────────────────────
# DI volumes use several header styles across different periods/editors:
#   "15. Mai 834."          day. MonthName Year.
#   "1341."                 year only
#   "1341. Júní 14."        year. MonthName day.
#   "1341, Júní 14."        year, MonthName day.  (some volumes use comma)
#   "[835]."                bracketed conjectural year
#   "[um 1100]."            bracketed circa
#   "[Vorið 1083.]"         bracketed season + year
#   "[eptir 1431]"          bracketed post-quem
#   "13. 24. Febrúar 1391. í Vík i Sæmundarhlíð."   full date + trailing place text
_YEAR = r"(?:8[3-9]\d|9\d\d|1[0-5]\d\d)"
_MONTH = r"[A-Za-zÀ-öø-ÿÞþÐðÆæÖö]{3,}"
_SEQ = r"(?:\d{1,4}\.\s+)?"        # optional leading charter-sequence number
_TRAIL_PREPS = r"(?:í|á|at|að|undir|fyrir)"
# Optional trailing PLACE text only — every confirmed real example is a short
# locative phrase ("í Vík i Sæmundarhlíð.", "á alþingi.", "at Helgafelli.").
# Deliberately does NOT allow arbitrary trailing text: a table-of-contents
# entry's trailing text is a full descriptive sentence ("Skrá um eignir
# Helgastaðakirkju, er Jón...") that happens to follow a bracketed date too,
# and an unbounded _TRAIL would misclassify those as real charter openers,
# bypassing the LLM safety net entirely (candidates get a second look;
# strict_open matches don't). Requiring a locative-preposition start and a
# short remainder keeps genuine trailing place text distinct from that.
_TRAIL = rf"(?:\s+{_TRAIL_PREPS}\s+\S.{{0,25}})?"

_HEADER_PATTERNS_ALWAYS = [
    # {seq}. {whitespace} MonthName Year.  (e.g. "3.     April 846." — DI layout format)
    re.compile(rf"^\s*\d{{1,3}}\.\s{{2,}}{_MONTH}\.?\s+{_YEAR}\.?\s*$"),
    # Year. MonthName day.  (e.g. "1341. Júní 14.")
    re.compile(rf"^\s*{_YEAR}[.,]\s+{_MONTH}\.?\s+\d{{1,2}}\.?\s*$"),
    # Year only — constrained to DI date range 834-1599
    re.compile(rf"^\s*{_YEAR}\.\s*$"),
    # [optional seq.] day. Month year. [optional trailing text]
    # e.g. "5. 31. Mai 858." or "13.  24. Febrúar 1391. í Vík i Sæmundarhlíð."
    re.compile(rf"^\s*{_SEQ}\d{{1,2}}\.\s+{_MONTH}\.?\s+{_YEAR}\.{_TRAIL}\s*$"),
    # [optional seq.] [year]. [optional trailing text] — bracketed conjectural year
    re.compile(rf"^\s*{_SEQ}\[{_YEAR}\]\.?{_TRAIL}\s*$"),
    # [optional seq.] [um year(-year)]. [optional trailing text] — bracketed circa
    re.compile(rf"^\s*{_SEQ}\[um\s+{_YEAR}(?:[—–-]{_YEAR})?\]\.?{_TRAIL}\s*$", re.IGNORECASE),
    # [optional seq.] [Month/season year.]. [optional trailing text] — bracketed month/season+year
    re.compile(rf"^\s*{_SEQ}\[{_MONTH}\s+{_YEAR}\.?\]\.?{_TRAIL}\s*$"),
    # [optional seq.] [eptir year]. [optional trailing text] — bracketed post-quem
    re.compile(rf"^\s*{_SEQ}\[eptir\s+{_YEAR}\]\.?{_TRAIL}\s*$", re.IGNORECASE),
    # [optional seq.] two independent bracket groups (conjectural date + conjectural place)
    re.compile(rf"^\s*{_SEQ}\[[^\]]*{_YEAR}[^\]]*\]\s*\[[^\]]+\]\.?{_TRAIL}\s*$"),
]

_HEADER_PATTERNS_GATED = [
    # Year range, e.g. "1341—1345." — only safe to treat as a charter opener
    # once a charter has already opened; DI volume title pages state the
    # whole volume's covering date range in exactly this shape.
    re.compile(rf"^\s*{_YEAR}[—–-]{_YEAR}\.?\s*$"),
]

FOOTNOTE_RE = re.compile(r"^\s*\d+\)\s")  # "1) footnote text"

# Table-of-contents / index lines use a dot-leader ("......") between a
# description and a trailing page number/range — a distinctive, reliable
# structural signal never seen in real charter headers or body text.
# Confirmed real example (vol.4 TOC): "54.   1397. Máldagi Sóttartungukirkju
# í Holtum......               62—63". Excluded unconditionally, before the
# candidate check, so a numbered TOC entry never gets queued for classification.
_DOT_LEADER_RE = re.compile(r"\.\s?\.\s?\.")

# Permissive candidate shapes: deliberately over-triggers. Any isolated,
# reasonably short line starting like a numbered/bracketed header might be a
# genuine new charter in a shape we haven't cataloged, or a registrum
# sub-entry — either way it's ambiguous enough to queue for classification
# rather than silently treating it as plain body text.
_ROMAN_NUMERAL_OPENER_RE = re.compile(r"^[IVXLCDM]{1,8}\.\s", re.IGNORECASE)
_DIGIT_OPENER_RE = re.compile(r"^\d{1,4}\.\s")
_BRACKET_OPENER_RE = re.compile(r"^\[")
_CANDIDATE_MAX_LEN = 90

CLASSIFY_BATCH_SIZE = 40

CLASSIFY_SYSTEM_PROMPT = """You classify candidate charter-boundary lines from an OCR
scan of a Diplomatarium Islandicum (DI) volume — a published edition of
medieval Icelandic charters dated roughly 834-1600.

Each candidate line was flagged because it LOOKS like it might be a header
(starts with a number, roman numeral, or bracket) but didn't match any of
the pipeline's known header shapes. For each one, decide exactly one of:

- "new_charter": a genuine new charter/document begins here. This INCLUDES
  an individually-named church/place entry inside a larger dated collection
  such as a bishop's máldagi/property survey (e.g. "XXXII. Willingahollt.",
  a roman-numeral heading naming one church within a multi-church
  visitation record) — DI's own table of contents gives each such entry its
  own item number and page range (e.g. "54. 1397. Máldagi Sóttartungukirkju
  í Holtum...... 62—63"), so DI itself treats these as separately citable
  documents, not as subordinate content of the collection they were
  recorded in. Classify these as new_charter even though they share the
  collection's overall date and that date may not appear on this exact line
  (the shared year is still readable from container_header and from the
  charter's own body text, so it isn't lost).
- "sub_entry": a numbered or lettered item that is genuinely NOT an
  independently-citable document — e.g. a continuation/witness-list
  fragment, or an appendix numbering scheme with no place/church name of
  its own. This should be rare; when a candidate names a specific place or
  church, prefer new_charter per the guidance above.
- "header_noise": a running header, page number, OCR artifact, or a table-
  of-contents/index entry (a numbered line summarizing a charter with its
  date and a page reference, from a "Röð og efni bréfanna"-style front-
  matter list, or a back-matter registr/index — container_header will
  usually be empty/"(none open yet)" for these, and the line itself often
  reads like a short blurb + page number rather than a document's own
  opening text).
- "uncertain": you genuinely cannot decide confidently from the context given.

DI's OCR has a confirmed, systematic 3<->8 digit misread specifically inside
bracketed/italic conjectural-year text (e.g. "[1878]" is often really
"[1378]"). If a candidate's line contains a bracketed year that looks
implausible (outside 834-1600) or inconsistent with its context_before/
context_after, set "suggested_year_correction" to your best-guess corrected
year — but ONLY as a suggestion for a human to confirm; never treat this as
settled fact, and set it to null whenever you aren't specifically flagging a
suspected digit misread.

Respond ONLY with a JSON array, one object per candidate, in this exact
shape, no prose, no markdown fences:
[{"id": 0, "classification": "new_charter|sub_entry|header_noise|uncertain",
  "suggested_year_correction": "YYYY or null", "confidence": "high|medium|low",
  "reasoning": "one short sentence"}]
"""


def is_charter_header(line: str, allow_year_range: bool = True) -> bool:
    """allow_year_range=False disables the year-range pattern (e.g.
    "834—1264."). DI volume title pages state the whole volume's covering
    date range in exactly this shape, which false-positives as a charter
    header when no charter has opened yet — see _collect_line_events()."""
    for pattern in _HEADER_PATTERNS_ALWAYS:
        if pattern.match(line):
            return True
    if allow_year_range:
        for pattern in _HEADER_PATTERNS_GATED:
            if pattern.match(line):
                return True
    return False


def _is_permissive_candidate(stripped: str) -> bool:
    if len(stripped) > _CANDIDATE_MAX_LEN:
        return False
    return bool(
        _ROMAN_NUMERAL_OPENER_RE.match(stripped)
        or _DIGIT_OPENER_RE.match(stripped)
        or _BRACKET_OPENER_RE.match(stripped)
    )


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


# ── Pass 1: tag every line, without deciding segmentation yet ──────────────


def _collect_line_events(pages: list[tuple[int, str]]) -> list[dict]:
    """Walk all pages/lines once, tagging each as:
      "blank"       — blank line (preserved so pass 3 can reproduce spacing)
      "page_header" — first non-blank line of a page; running header/footer,
                       always excluded regardless of shape
      "strict_open" — unconditionally matches a known charter-header shape
      "footnote"    — footnote-marker line, excluded
      "toc_entry"   — a dot-leader line (table of contents/index), excluded
      "candidate"   — permissive-shaped but not strictly matched; ambiguous,
                       classification pending (see classify_candidates())
      "body"        — plain text
    Doesn't decide segmentation — see classify_candidates() and _assemble_charters().

    A candidate additionally requires at least one blank line immediately
    before it. Confirmed real counter-example without this: numbered legal
    clauses inside a continuous law text (e.g. DI's Tíundarlög) run with
    ZERO blank lines between consecutive items ("1. þat er mælt..." directly
    followed by "2. þat fe þarf..." on the very next line) — genuine charter
    openers and registrum sub-entries are always set off by blank-line
    spacing, so this cheaply excludes ordinary in-paragraph enumeration
    without needing to recognize "this is a law text" specifically."""
    events = []
    opened_any = False
    for page_num, page_text in pages:
        lines = page_text.splitlines()
        seen_content = False
        blank_run = 0
        for line in lines:
            stripped = line.strip()
            if not stripped:
                events.append({"tag": "blank"})
                blank_run += 1
                continue

            is_page_top = not seen_content
            seen_content = True

            if is_page_top:
                events.append({"tag": "page_header", "page_num": page_num, "line": stripped})
                blank_run = 0
                continue

            if _DOT_LEADER_RE.search(stripped):
                events.append({"tag": "toc_entry", "page_num": page_num, "line": stripped})
                blank_run = 0
                continue

            if is_charter_header(stripped, allow_year_range=opened_any):
                events.append({"tag": "strict_open", "page_num": page_num, "line": stripped})
                opened_any = True
                blank_run = 0
                continue

            if FOOTNOTE_RE.match(line):
                events.append({"tag": "footnote"})
                blank_run = 0
                continue

            if blank_run >= 1 and _is_permissive_candidate(stripped):
                events.append({"tag": "candidate", "page_num": page_num, "line": stripped})
                blank_run = 0
                continue

            events.append({"tag": "body", "line": line})
            blank_run = 0
    return events


def _context_for_candidate(events: list[dict], idx: int, n: int = 3) -> dict:
    """Gathers a little surrounding text and the nearest preceding strict-open
    header (the document a sub-entry would belong to, if it is one) so each
    candidate's classification prompt is self-contained -- no cross-batch
    state needs to be tracked."""
    def _nearby_text(rng):
        out = []
        for e in rng:
            if e["tag"] == "body":
                out.append(e["line"].strip())
            elif e["tag"] == "strict_open":
                out.append(e["line"])
        return " / ".join(out[-n:]) if out else ""

    before = _nearby_text(events[max(0, idx - 10):idx])
    after = _nearby_text(events[idx + 1:idx + 11])

    container_header = ""
    for e in reversed(events[:idx]):
        if e["tag"] == "strict_open":
            container_header = e["line"]
            break

    return {"context_before": before, "context_after": after, "container_header": container_header}


# ── Pass 2: batch-classify ambiguous candidates via Claude ──────────────────


def _fallback_uncertain(reason: str) -> dict:
    return {"classification": "uncertain", "suggested_year_correction": None,
            "confidence": "low", "reasoning": reason}


def _classify_batch(client: anthropic.Anthropic, batch: list[dict], vol_num: int) -> list[dict]:
    items = [
        {
            "id": i,
            "line": c["line"],
            "context_before": c["context_before"],
            "context_after": c["context_after"],
            "container_header": c["container_header"] or "(none open yet)",
        }
        for i, c in enumerate(batch)
    ]
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=[{"type": "text", "text": CLASSIFY_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": f"Volume {vol_num}. Classify these "
                                                    f"{len(items)} candidate lines:\n\n"
                                                    f"{json.dumps(items, ensure_ascii=False, indent=2)}"}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        parsed = json.loads(raw)
        by_id = {d["id"]: d for d in parsed}
        return [by_id.get(i, _fallback_uncertain("missing from classification response"))
                for i in range(len(batch))]
    except Exception as e:
        print(f"[vol {vol_num}] WARNING: candidate classification call failed ({e}); "
              f"treating {len(batch)} candidate(s) as uncertain.")
        return [_fallback_uncertain(str(e)) for _ in batch]


def classify_candidates(candidates: list[dict], vol_num: int) -> list[dict]:
    """Batch-classifies permissive-but-not-strictly-matched candidate lines,
    ~CLASSIFY_BATCH_SIZE per call. Returns one classification dict per
    candidate, same order. Skips the API entirely if there are none."""
    if not candidates:
        return []

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print(f"[vol {vol_num}] WARNING: ANTHROPIC_API_KEY not set -- cannot classify "
              f"{len(candidates)} ambiguous candidate line(s); treating them all as "
              f"uncertain and flagging for review.")
        return [_fallback_uncertain("ANTHROPIC_API_KEY not set") for _ in candidates]

    client = anthropic.Anthropic(api_key=api_key)
    results = []
    for batch_start in range(0, len(candidates), CLASSIFY_BATCH_SIZE):
        batch = candidates[batch_start:batch_start + CLASSIFY_BATCH_SIZE]
        results.extend(_classify_batch(client, batch, vol_num))
    return results


# ── Pass 3: assemble final charter segments from resolved events ───────────


def _assemble_charters(events: list[dict]) -> tuple[list[dict], list[dict]]:
    """Returns (charters, flagged_rows). charters: list of {seq, page_start,
    date_header, text}. flagged_rows: uncertain classifications and suggested
    OCR year corrections, for flagged_for_review.csv -- never auto-applied."""
    charters: list[dict] = []
    flagged_rows: list[dict] = []
    current: dict | None = None

    for e in events:
        tag = e["tag"]

        if tag == "blank":
            if current:
                current["text"] += "\n"
            continue

        if tag in ("page_header", "footnote", "toc_entry"):
            continue

        if tag == "strict_open":
            if current:
                charters.append(current)
            current = {"seq": len(charters) + 1, "page_start": e["page_num"],
                       "date_header": e["line"], "text": e["line"] + "\n"}
            continue

        if tag == "candidate":
            resolved = e["resolved"]
            cls = resolved["classification"]
            correction = resolved.get("suggested_year_correction")

            if cls == "new_charter":
                if current:
                    charters.append(current)
                current = {"seq": len(charters) + 1, "page_start": e["page_num"],
                           "date_header": e["line"], "text": e["line"] + "\n"}
            elif cls == "header_noise":
                pass  # excluded entirely, like a page_header
            else:  # sub_entry or uncertain -> body text of whatever's open
                if current is not None:
                    current["text"] += e["line"] + "\n"

            if cls == "uncertain" or correction:
                flagged_rows.append({
                    "page_num": e["page_num"], "line": e["line"], "classification": cls,
                    "confidence": resolved.get("confidence", ""),
                    "suggested_year_correction": correction or "",
                    "reasoning": resolved.get("reasoning", ""),
                })
            continue

        if tag == "body":
            if current is not None:
                current["text"] += e["line"] + "\n"

    if current:
        charters.append(current)

    return charters, flagged_rows


def segment_volume(pages: list[tuple[int, str]], vol_num: int) -> tuple[list[dict], list[dict]]:
    """
    Split a volume's text into individual charter blocks.
    Returns (charters, flagged_rows) -- see _assemble_charters().
    """
    events = _collect_line_events(pages)

    candidate_events = [e for e in events if e["tag"] == "candidate"]
    for idx, e in enumerate(events):
        if e["tag"] == "candidate":
            e.update(_context_for_candidate(events, idx))

    if candidate_events:
        print(f"[vol {vol_num}] {len(candidate_events)} ambiguous candidate line(s) "
              f"found -- classifying via Claude...")
        results = classify_candidates(candidate_events, vol_num)
        for e, r in zip(candidate_events, results):
            e["resolved"] = r

    return _assemble_charters(events)


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

    charters, flagged_rows = segment_volume(pages, vol_num)
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

    # Always resolve flagged_for_review.csv's state (write fresh, or remove a
    # stale one) rather than only writing when non-empty -- classification
    # calls aren't perfectly deterministic across runs, so a re-run that
    # happens to flag nothing must not leave a PRIOR run's stale flagged
    # rows sitting on disk looking current.
    flagged_path = vol_dir / "flagged_for_review.csv"
    if flagged_rows:
        with open(flagged_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["page_num", "line", "classification",
                                                     "confidence", "suggested_year_correction", "reasoning"])
            writer.writeheader()
            writer.writerows(flagged_rows)
        print(f"[vol {vol_num}] {len(flagged_rows)} line(s) flagged for review → {flagged_path.name}")
    elif flagged_path.exists():
        flagged_path.unlink()

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
