"""
Cross-volume/authority person duplicate candidate finder.

Unlike the numbered per-volume pipeline steps, this is a cross-cutting tool
meant to be re-run periodically as more volumes get processed -- it has no
--vol argument, since its whole point is comparing across everything.

Usage:
    python 07_find_person_duplicates.py

Reads:  person_names_authority.csv (already-promoted persons)
        output/review/vol*_persons_new.csv (every volume processed so far)
Writes: output/review/cross_volume_person_duplicates.csv
        — one row per candidate pair, blank `decision` column for manual
        triage. Never auto-merges anything; confirming a pair as the same
        person here does not touch any other file. Applying a confirmed
        merge (relinking charter references, removing the duplicate row)
        is a separate, manual follow-up step.

Strategy:
  1. Load every known person (authority + all volumes' not-yet-promoted
     new-person candidates) into one pool, each tagged with its source.
  2. Compare every pair exhaustively -- no blocking. A first-letter block
     was tried and dropped during development: it would have excluded a
     confirmed real case ("Þórunn" vs "Jórunn", a Þ/J OCR misread seen in
     real DI data) that lands in different blocks under any accent-folding
     scheme. At this corpus's scale (low hundreds of candidates),
     exhaustive comparison is trivial; only worth revisiting if scale
     (many more volumes processed) makes it a real bottleneck.
  3. Score name similarity with rapidfuzz.fuzz.token_sort_ratio across all
     name forms (canonical + variants) on both sides. Two-tier threshold,
     not one: measured against real DI names, genuine spelling variants of
     the same person scored 90-100, while different people sharing a
     common first name and similar-sounding patronymic (different fathers)
     scored as high as 87.2 -- close enough to a naive single cutoff that
     it needs its own margin (PERSON_DUP_NAME_HIGH=90) plus a separate,
     explicitly lower-confidence "possible" tier (PERSON_DUP_NAME_MEDIUM=78)
     rather than one line that either misses real variants or wrongly
     promotes near-miss different-person pairs to "likely".
  4. Separately handle the "one name is bare, the other has a patronymic/
     surname" case (a lot of individuals are mentioned with fewer names in
     one charter than another) -- token_sort_ratio alone scores this very
     low (e.g. "Jón" vs "Jón Þorláksson" ~35), so it needs its own narrow,
     structural check (first-token exact match) rather than a fuzzy
     blanket threshold. This is deliberately NOT scored via WRatio's
     partial-ratio behavior -- WRatio gives "Jón" a flat ~90 against every
     different "Jón X" in the corpus regardless of X, which would flood
     the output given how common single given names are here. The bare-
     name case is always flagged at low confidence, never "likely", since
     a bare given name alone is genuinely ambiguous.
  5. Cross-check floruit_start/floruit_end with a +/-30 year tolerance
     (PERSON_DUP_DATE_TOLERANCE_YEARS) as a corroborating/demoting signal,
     not a hard gate: a strong name match with dates that don't overlap
     even after the tolerance is flagged as "name_match_date_conflict"
     (worth extra scrutiny) rather than silently upgraded to "likely" or
     silently dropped. Missing dates on either side are treated as
     "unknown", not as a conflict -- most currently-generated data
     predates floruit dates being populated at all.
"""

import re
import sys
from dataclasses import dataclass
from pathlib import Path

from rapidfuzz import fuzz

sys.path.insert(0, str(Path(__file__).parent))
from config import PERSON_DUP_NAME_HIGH, PERSON_DUP_NAME_MEDIUM, PERSON_DUP_DATE_TOLERANCE_YEARS
import db
from db import split_variants
from person_authority import _int_or_blank

_PAREN_TAIL = re.compile(r"\s*\([^)]*\)\s*$")


# ── Normalization ────────────────────────────────────────────────────────


def normalize_name(name: str) -> str:
    if not name:
        return ""
    n = _PAREN_TAIL.sub("", name).strip().lower()
    n = re.sub(r"\s+", " ", n)
    n = re.sub(r"[.,;:]", "", n)
    return n


# ── Data loading ─────────────────────────────────────────────────────────


@dataclass
class PersonCandidate:
    id: int              # person_pk (was display_id string pre-migration)
    canonical_name: str
    variants: list
    floruit_start: str
    floruit_end: str
    occupation: str
    title: str
    source: str  # "authority" or e.g. "vol04"


def load_candidates() -> list:
    """Loads every person (canonical authority + every volume's provisional
    rows) in one query -- simpler than the old two-source load (PersonAuthority
    + a per-volume CSV glob), since everything is one table now."""
    candidates = []
    df = db.get_persons()  # all statuses, all volumes
    for row in df.to_dict("records"):
        source = "authority" if row["status"] == "canonical" else f"vol{int(row['source_volume']):02d}"
        candidates.append(PersonCandidate(
            id=row["person_pk"], canonical_name=row["canonical_name"],
            variants=split_variants(row.get("variant_names") or ""),
            floruit_start=_int_or_blank(row.get("floruit_start")),
            floruit_end=_int_or_blank(row.get("floruit_end")),
            occupation=row.get("occupation") or "", title=row.get("title") or "",
            source=source,
        ))
    return candidates


def name_forms(c: PersonCandidate) -> list:
    forms = [normalize_name(c.canonical_name)] + [normalize_name(v) for v in c.variants]
    return [f for f in dict.fromkeys(forms) if f]


# ── Matching ─────────────────────────────────────────────────────────────


def best_name_score(a: PersonCandidate, b: PersonCandidate) -> int:
    best = 0
    for fa in name_forms(a):
        for fb in name_forms(b):
            score = fuzz.token_sort_ratio(fa, fb)
            if score > best:
                best = score
    return round(best)


def is_bare_name_subset(a: PersonCandidate, b: PersonCandidate) -> bool:
    """True if either side is a single bare token (no patronymic/surname
    visible) that exactly matches the first token of the other's name."""
    na = normalize_name(a.canonical_name)
    nb = normalize_name(b.canonical_name)
    a_tokens, b_tokens = na.split(), nb.split()
    if len(a_tokens) == 1 and len(b_tokens) > 1:
        return a_tokens[0] == b_tokens[0]
    if len(b_tokens) == 1 and len(a_tokens) > 1:
        return b_tokens[0] == a_tokens[0]
    return False


def _year(s: str):
    s = (s or "").strip()
    return int(s) if s.lstrip("-").isdigit() else None


def date_status(a: PersonCandidate, b: PersonCandidate, tolerance: int) -> str:
    """'overlap' | 'conflict' | 'unknown'. Uses whichever of floruit_start/
    floruit_end is available on each side (both are set to the same
    single-point charter-attested year for newly-minted persons)."""
    a_start = _year(a.floruit_start) or _year(a.floruit_end)
    a_end = _year(a.floruit_end) or _year(a.floruit_start)
    b_start = _year(b.floruit_start) or _year(b.floruit_end)
    b_end = _year(b.floruit_end) or _year(b.floruit_start)
    if a_start is None or b_start is None:
        return "unknown"
    a_lo, a_hi = a_start - tolerance, a_end + tolerance
    b_lo, b_hi = b_start - tolerance, b_end + tolerance
    return "overlap" if a_lo <= b_hi and b_lo <= a_hi else "conflict"


def classify_pair(a: PersonCandidate, b: PersonCandidate) -> dict | None:
    name_score = best_name_score(a, b)
    bare_subset = is_bare_name_subset(a, b)
    dstatus = date_status(a, b, PERSON_DUP_DATE_TOLERANCE_YEARS)

    if name_score >= PERSON_DUP_NAME_HIGH:
        if dstatus == "conflict":
            classification, confidence = "name_match_date_conflict", "medium"
        else:
            classification, confidence = "likely_duplicate", "high"
    elif name_score >= PERSON_DUP_NAME_MEDIUM:
        if dstatus == "conflict":
            return None  # mediocre name score AND incompatible dates -- not worth surfacing
        classification, confidence = "possible_duplicate", "medium"
    elif bare_subset and dstatus != "conflict":
        classification, confidence = "possible_duplicate_bare_name", "low"
    else:
        return None

    return {
        "a_id": a.id, "a_name": a.canonical_name, "a_source": a.source,
        "a_floruit": f"{a.floruit_start}-{a.floruit_end}".strip("-"),
        "a_occupation": a.occupation, "a_title": a.title,
        "b_id": b.id, "b_name": b.canonical_name, "b_source": b.source,
        "b_floruit": f"{b.floruit_start}-{b.floruit_end}".strip("-"),
        "b_occupation": b.occupation, "b_title": b.title,
        "name_score": name_score, "date_status": dstatus,
        "classification": classification, "confidence": confidence,
        "decision": "",
    }


def find_duplicates(candidates: list) -> list:
    """Exhaustive pairwise comparison -- no blocking. A first-letter block
    was tried and dropped: it would have excluded a confirmed real case
    ("Þórunn" vs "Jórunn", a Þ/J OCR misread seen in real DI data) that
    lands in different blocks under any accent-folding scheme. At this
    corpus's scale (low hundreds of candidates), exhaustive comparison is
    computationally trivial; revisit blocking only if scale (many more
    volumes processed) makes it a real bottleneck."""
    seen_pairs = set()
    rows = []
    for i, a in enumerate(candidates):
        for b in candidates[i + 1:]:
            if a.id == b.id and a.source == b.source:
                continue
            pair_key = tuple(sorted([f"{a.source}:{a.id}", f"{b.source}:{b.id}"]))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            row = classify_pair(a, b)
            if row:
                rows.append(row)

    # Highest-confidence, most-actionable rows first
    order = {"likely_duplicate": 0, "name_match_date_conflict": 1,
             "possible_duplicate": 2, "possible_duplicate_bare_name": 3}
    rows.sort(key=lambda r: (order.get(r["classification"], 9), -r["name_score"]))
    return rows


def main():
    candidates = load_candidates()
    print(f"Loaded {len(candidates)} person candidates "
          f"({sum(1 for c in candidates if c.source == 'authority')} from authority, "
          f"{sum(1 for c in candidates if c.source != 'authority')} new-entity rows across "
          f"{len({c.source for c in candidates if c.source != 'authority'})} volume(s)).")

    rows = find_duplicates(candidates)

    if not rows:
        print("No duplicate candidates found.")
        return

    # person_a_pk/person_b_pk instead of a_id/b_id -- db.upsert_person_duplicate_candidates()
    # never overwrites a non-blank decision on re-run (fixes this script's old
    # behavior of silently wiping a human's recorded same/different decision
    # every time it was re-run).
    db_rows = [{
        "person_a_pk": r["a_id"], "person_b_pk": r["b_id"], "name_score": r["name_score"],
        "date_status": r["date_status"], "classification": r["classification"],
        "confidence": r["confidence"],
    } for r in rows]
    result = db.upsert_person_duplicate_candidates(db_rows)

    by_class = {}
    for r in rows:
        by_class[r["classification"]] = by_class.get(r["classification"], 0) + 1
    print(f"Upserted {len(db_rows)} candidate pair(s) into person_duplicate_candidates "
          f"({result['inserted']} new, {result['updated_or_unchanged']} refreshed/unchanged): "
          + ", ".join(f"{k}={v}" for k, v in by_class.items()))


if __name__ == "__main__":
    main()
