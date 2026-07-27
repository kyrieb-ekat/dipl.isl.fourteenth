"""
Flag persons who are probably later manuscript-transmission actors (a
copyist/editor/annotator from centuries after a charter's own date) mis-minted
as if they were period-contemporary persons of that charter.

Why this exists: 02_extract_entities.py's system prompt lists "scribe" as a
valid role_category for the generic persons array, and DI's own editorial
apparatus routinely documents WHO transcribed a surviving later copy of an
older document (e.g. "Árni Magnússon's scribe, attested 1712" for a charter
dated ~1150) -- real text, correctly read, but placed in the wrong schema
slot. 03_resolve_entities.py then mints a provisional person for any
scribe-role entry not already in the authority file and stamps
floruit_start=floruit_end= the CHARTER's own date unconditionally, producing
a person whose only textual evidence is a much later date than their
floruit claims.

Confirmed via investigation this is NOT the common case for occupation=
'scribe' -- most such rows are genuine period-contemporary notaries/
chancellors, correctly dated to their own document. This script flags,
it never deletes or auto-corrects: some cases are genuinely ambiguous
(e.g. a common name recurring across genuinely different real people from
different eras, like several distinct papal chancellors named "Leo") and
deserve a human look in the Review app, not a one-way bulk action.

Usage:
    python 09_flag_transmission_actors.py              # dry run (default)
    python 09_flag_transmission_actors.py --confirm     # actually write flags

Scans ALL occupation='scribe' persons regardless of status/review_status
-- deliberately not scoped to status='provisional' or review_status=''
-- so a row already reviewed or promoted to canonical this session still
gets caught and flagged for a second look. Writes ONLY the
data_quality_flag column; never touches status, review_status, or any
other field, and never deletes anything.

Two combined signals (an explicit title match alone is high-precision but
undercounts -- confirmed empirically against Árni Magnússon's 44 separate
person rows, most of which don't contain an obvious keyword):

1. Title/qualifier text matching copy/copyist/transcribed/annotation/
   editor/discovered/referenced-in patterns, or an attested date in a
   later century than the charter's own date.
2. occupation='scribe' AND appears in a person_duplicate_candidates row
   already classified 'name_match_date_conflict' (run 07_find_person_
   duplicates.py first if it hasn't run against current data) -- catches
   recurring-name clusters with incompatible floruit regardless of title
   wording. Restricted to occupation='scribe' specifically (not any
   name-match-date-conflict pair) to avoid false-flagging genuinely
   different real people who happen to share a common name across
   centuries -- confirmed empirically this still lets through some
   legitimate same-name-different-era people (e.g. several distinct papal
   chancellors named "Leo"), which is acceptable given this only flags for
   a human glance, never a deletion.
"""
import argparse
import re

import db

_TITLE_PATTERNS = [
    r"\bcop(y|ied|yist)\b", r"\btranscri", r"\bannotat", r"\beditor\b",
    r"\bdiscover", r"\breferenced in\b", r"\bmarginal\b", r"\bafskr",
    r"\battested\s+1[6-9]\d\d\b", r"\b1[6-9]\d\d\b",
]
_TITLE_RE = re.compile("|".join(_TITLE_PATTERNS), re.IGNORECASE)

FLAG_VALUE = "later_transmission_actor"


def _signal_1_title_match(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT person_pk, canonical_name, title, floruit_start, floruit_end, "
        "status, review_status, data_quality_flag FROM persons WHERE occupation='scribe'"
    ).fetchall()
    return [dict(r) for r in rows if r["title"] and _TITLE_RE.search(r["title"])]


def _signal_2_date_conflict(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT DISTINCT p.person_pk, p.canonical_name, p.title, p.floruit_start,
                  p.floruit_end, p.status, p.review_status, p.data_quality_flag
           FROM persons p
           JOIN person_duplicate_candidates c ON p.person_pk IN (c.person_a_pk, c.person_b_pk)
           WHERE p.occupation='scribe' AND c.classification='name_match_date_conflict'"""
    ).fetchall()
    return [dict(r) for r in rows]


def find_candidates() -> dict:
    conn = db.get_connection()
    try:
        s1 = {r["person_pk"]: r for r in _signal_1_title_match(conn)}
        s2 = {r["person_pk"]: r for r in _signal_2_date_conflict(conn)}
    finally:
        conn.close()
    merged = {**s2, **s1}  # signal 1 (higher precision) wins on overlap, doesn't matter which since same row
    for pk, row in merged.items():
        row["signals"] = ("title" if pk in s1 else "") + ("+date_conflict" if pk in s2 else "")
        row["signals"] = row["signals"].strip("+")
    return merged


def flag(candidates: dict) -> int:
    conn = db.get_connection()
    try:
        with conn:
            for pk in candidates:
                conn.execute(
                    "UPDATE persons SET data_quality_flag=? WHERE person_pk=?",
                    (FLAG_VALUE, pk),
                )
    finally:
        conn.close()
    return len(candidates)


def main():
    parser = argparse.ArgumentParser(
        description="Flag persons likely to be later manuscript-transmission actors, not period-contemporary people."
    )
    parser.add_argument("--confirm", action="store_true",
                         help="Actually write the flag. Without this, only prints what would be flagged.")
    args = parser.parse_args()

    candidates = find_candidates()
    already_flagged = sum(1 for r in candidates.values() if r["data_quality_flag"])
    print(f"{len(candidates)} candidate(s) found ({already_flagged} already flagged, "
          f"{len(candidates) - already_flagged} new).")
    for pk, r in sorted(candidates.items(), key=lambda kv: kv[1]["canonical_name"]):
        print(f"  [{r['signals']:>18}] {r['canonical_name']!r:30} "
              f"floruit={r['floruit_start']}-{r['floruit_end']}  "
              f"status={r['status']}/{r['review_status'] or '-'}  "
              f"title={r['title'][:60]!r}")

    if not args.confirm:
        print("\nDry run only -- pass --confirm to actually write data_quality_flag.")
        return

    n = flag(candidates)
    print(f"\nFlagged {n} person(s) with data_quality_flag='{FLAG_VALUE}'.")


if __name__ == "__main__":
    main()
