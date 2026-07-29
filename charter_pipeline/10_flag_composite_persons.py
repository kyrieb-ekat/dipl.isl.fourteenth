"""
Flag person records that are over-merged composites -- ONE id holding several
real people.

This is the inverse of the duplication problem the rest of the pipeline hunts.
07_find_person_duplicates.py asks "are these two records the same person?";
this asks the prior question "is this record even one person?", using only the
record's own charter attestations.

Why it matters, concretely. Every composite found so far is an authority-file
import with status='canonical', which means db.search_authority offers it as a
merge target at score 100 to every new extraction sharing the bare given
name -- so the bucket keeps growing. p027 "Jón" is attested across 52 charters
from 1180 to 1488 as an archbishop, a bishop, a priest, a layman AND a patron
saint. It is not a person; it is what the name "Jón" has been collecting.
80 open duplicate candidates currently point at one of these records, and
merging into an unsplit composite always makes it worse.

Two severity tiers, deliberately not one boolean -- conflating a fact with a
guess would force the reviewer to treat them identically:

  certain  Physically impossible. An attestation span longer than a lifetime
           (>100 years), or a patron saint sharing an id with a living actor.
           Both confirmed against real records: p006 merges St Lawrence with a
           contemporary Abbot Laurentius (both 1247, so span alone would never
           catch it); v01-p016 merges several Pope Gregorys with St Gregory
           across 846-1044.
  review   Suspicious but arguable. Mutually exclusive roles inside a
           plausible lifespan can equally be loose role labelling by the
           extractor -- p030 is issuer-bishop and issuer-layman within one
           year, which may be one bishop or two people.

Like 09_flag_transmission_actors.py this only ever WRITES data_quality_flag,
never status/review_status, and never deletes or auto-splits anything.
Splitting a composite -- partitioning its attestations into separate records
and relinking them -- is a separate, human-driven operation.

The flag is accumulated as a semicolon set (db.add_data_quality_flag), so a
record can be both a later-transmission actor and a composite without either
value clobbering the other.

Usage:
    python 10_flag_composite_persons.py                  # dry run (default)
    python 10_flag_composite_persons.py --confirm         # write flags
    python 10_flag_composite_persons.py --severity review # include arguable ones
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import db


def main():
    parser = argparse.ArgumentParser(
        description="Flag person records that hold more than one real person."
    )
    parser.add_argument("--confirm", action="store_true",
                        help="Actually write the flag. Without this, only prints "
                             "what would be flagged.")
    parser.add_argument("--severity", choices=["certain", "review"], default="certain",
                        help="'certain' (default) flags only physically impossible "
                             "records; 'review' also includes arguable ones.")
    args = parser.parse_args()

    problem = db.check_database()
    if problem:
        print(f"Database problem: {problem}")
        return 1

    rows = db.find_composite_persons()
    wanted = rows if args.severity == "review" \
        else [r for r in rows if r["severity"] == "certain"]

    certain = sum(1 for r in rows if r["severity"] == "certain")
    already = sum(1 for r in wanted if db.COMPOSITE_FLAG in (r["data_quality_flag"] or ""))

    print(f"{len(rows)} composite record(s) found "
          f"({certain} certain, {len(rows) - certain} needing review).")
    if args.severity == "certain" and len(rows) > certain:
        print(f"Showing the {certain} certain one(s); "
              f"pass --severity review to include the rest.")
    print(f"{len(wanted) - already} of {len(wanted)} shown are not yet flagged.\n")

    for r in wanted:
        origin = "authority" if r["source_volume"] is None else f"vol{r['source_volume']:02d}"
        years = f"{r['year_min']}-{r['year_max']}" if r["year_min"] is not None else "undated"
        print(f"  [{r['severity']:>7}] {r['display_id']:>10} {str(r['canonical_name'])[:24]!r:26} "
              f"{r['charters']:>3} charters  {years:>11}  [{origin}]"
              f"{'  (already flagged)' if db.COMPOSITE_FLAG in (r['data_quality_flag'] or '') else ''}")
        for f in r["findings"]:
            print(f"              - {f['reason']}")

    if not args.confirm:
        print(f"\nDry run only -- pass --confirm to write "
              f"data_quality_flag='{db.COMPOSITE_FLAG}'.")
        return 0

    n = 0
    for r in wanted:
        if db.COMPOSITE_FLAG in (r["data_quality_flag"] or ""):
            continue
        db.add_data_quality_flag(r["person_pk"], db.COMPOSITE_FLAG)
        n += 1
    print(f"\nFlagged {n} person(s) with data_quality_flag='{db.COMPOSITE_FLAG}' "
          f"(existing flag values preserved).")
    print("These must be resolved before cluster-merging: merging into an "
          "unsplit composite makes it worse.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
