"""Dashboard: where all the data currently sits, and what to do next.

Nothing in the app answered this before -- judging remaining work meant
opening each grid and reading its row count, and the two numbers that matter
most (how many places actually have coordinates, how many contradictions
exist) weren't visible anywhere at all.

The funnel is ordered the way the work has to happen, which is NOT the order
the pipeline scripts run in: deduplicate before accepting, because accepting
five spellings of one farm individually creates five authority entries that
then have to be merged back.
"""
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402


def _stage(label: str, done: int, total: int, *, help_text: str = "") -> None:
    pct = (done / total * 100) if total else 0.0
    st.progress(min(pct / 100, 1.0), text=f"**{label}** — {done:,} / {total:,} ({pct:.0f}%)")
    if help_text:
        st.caption(help_text)


def render(goto=None) -> None:
    """`goto(page_key)` is supplied by the entrypoint so the "start here"
    buttons can switch pages; omitted in tests."""
    c = db.get_funnel_counts()

    st.subheader("Where the data stands")

    top = st.columns(4)
    top[0].metric("Charters", f"{c['charters']:,}")
    top[1].metric("Persons", f"{c['persons_total']:,}",
                  f"{c['persons_canonical']:,} in authority")
    top[2].metric("Places", f"{c['places_total']:,}",
                  f"{c['places_canonical']:,} in authority")
    top[3].metric("Places geocoded", f"{c['places_geocoded']:,}",
                  f"of {c['places_total']:,}", delta_color="off")

    st.markdown("---")
    st.markdown("#### Pipeline stages")

    _stage("Charter references resolved",
           c["review_items_resolved"],
           c["review_items_resolved"] + c["review_items_open"] + c["review_items_decided"],
           help_text="Fuzzy matches in the 60–85 band that need accept/reject. "
                     f"{c['review_items_decided']:,} decided but not yet applied.")

    _stage("Person duplicates triaged",
           c["person_dups_total"] - c["person_dups_open"], c["person_dups_total"],
           help_text="Counted as raw pairs. Clustering collapses these to a few "
                     "hundred decisions — see the Review page.")

    _stage("Place ↔ nafnid candidates triaged",
           c["place_dups_total"] - c["place_dups_open"], c["place_dups_total"],
           help_text="Counted as raw candidate rows; each place carries ~5, of which "
                     "at most one can be right.")

    _stage("Persons reviewed",
           c["persons_total"] - c["persons_unreviewed"], c["persons_total"])
    _stage("Places reviewed",
           c["places_total"] - c["places_unreviewed"], c["places_total"])
    _stage("Places geocoded", c["places_geocoded"], c["places_total"],
           help_text="The nodegoat export needs coordinates, so this is the stage "
                     "that actually gates the end goal.")

    st.markdown("---")
    st.markdown("#### Needs attention")

    problems = []
    if c["places_contradictory"]:
        problems.append(
            f"**{c['places_contradictory']} place(s) have more than one confirmed nafnid "
            "match.** Those are mutually exclusive — different farms sharing a name — so "
            "they are left ungeocoded until resolved.")
    if c["charters_parse_error"]:
        problems.append(f"**{c['charters_parse_error']} charter(s) failed to parse** and are "
                        "flagged rather than silently wrong.")
    if c["persons_flagged"]:
        problems.append(
            f"**{c['persons_flagged']} person(s) flagged as later transmission actors** "
            "(copyists/annotators from centuries after the charter). Resolve these before "
            "merging person clusters, or they get folded into the medieval prosopography.")
    if c["charters_flagged"]:
        problems.append(f"{c['charters_flagged']:,} charter(s) still carry unresolved "
                        "person/place references.")

    if problems:
        for p in problems:
            st.warning(p, icon="⚠️")
    else:
        st.success("Nothing flagged.")

    st.markdown("---")
    st.markdown("#### Start here")

    # Biggest remaining bucket first -- the point is to remove the "which of
    # nine tabs do I open" decision.
    buckets = [
        ("Person duplicates", c["person_dups_open"], "review"),
        ("Place ↔ nafnid candidates", c["place_dups_open"], "review"),
        ("Charter references", c["review_items_open"], "review"),
        ("Unreviewed persons", c["persons_unreviewed"], "review"),
        ("Unreviewed places", c["places_unreviewed"], "review"),
    ]
    buckets = [b for b in buckets if b[1] > 0]
    buckets.sort(key=lambda b: -b[1])

    if not buckets:
        st.success("No pending decisions. 🎉")
        return

    biggest = buckets[0]
    st.caption(f"Largest remaining bucket: **{biggest[0]}** ({biggest[1]:,} items).")
    if goto and st.button(f"Review {biggest[0].lower()} →", type="primary",
                          key="dash_start_here"):
        goto(biggest[2])

    with st.expander("All pending buckets"):
        for name, count, _ in buckets:
            st.write(f"- {name}: **{count:,}**")
