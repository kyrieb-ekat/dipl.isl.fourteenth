"""
Character-level diff rendering for comparing two entity field values (e.g. a
provisional person's canonical_name vs. its proposed authority match).
Replaces the old compare panel's bare equal/not-equal (✓/≠) indicator --
highlights exactly which characters differ, since near-miss OCR spelling
variants ("Ólafur" vs "Olafur") are the common case a reviewer actually needs
to *see*, not just be told about.
"""
import difflib
import html

import pandas as pd
import streamlit as st

DIFF_CSS = """
<style>
.diff-del { background: #ffecec; text-decoration: line-through; color: #b00; border-radius: 2px; padding: 0 1px; }
.diff-add { background: #eaffea; color: #070; border-radius: 2px; padding: 0 1px; }
</style>
"""

# Field spec per entity type: (label, left-side field name(s), right-side
# field name(s)). A tuple of field names (e.g. floruit_start/end,
# coordinates_lat/long) is joined with " -- " before diffing, same convention
# the pre-diff compare panel used.
COMPARE_ROWS = {
    "person": [
        ("Name", "canonical_name", "canonical_name"),
        ("Variants", "variant_names", "variant_names"),
        ("Occupation", "occupation", "occupation"),
        ("Title", "title", "title"),
        ("Floruit", ("floruit_start", "floruit_end"), ("floruit_start", "floruit_end")),
        ("Notes", "notes", "notes"),
        ("Sources", "sources", "sources"),
    ],
    "place": [
        ("Name", "canonical_name", "canonical_name"),
        ("Variants", "variant_names", "variant_names"),
        ("Type", "place_type", "place_type"),
        ("Region", "region", "region"),
        ("Coordinates", ("coordinates_lat", "coordinates_long"), ("coordinates_lat", "coordinates_long")),
        ("Notes", "notes", "notes"),
        ("Sources", "sources", "sources"),
    ],
}


def blank(v) -> str:
    """Values sourced from a pandas DataFrame (e.g. db.search_authority) come
    back as NaN, which is truthy in Python -- `v or ""` doesn't catch it and
    str(nan) renders the literal text "nan". Values straight from a
    sqlite3.Row are already proper None, where this is a no-op."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return str(v)


def field_value(row: dict, keys) -> str:
    """Resolves a COMPARE_ROWS field spec (single key or tuple of keys)
    against a row dict, joining tuple fields with " -- "."""
    if isinstance(keys, tuple):
        return " -- ".join(blank(row.get(k)) for k in keys)
    return blank(row.get(keys))


def char_diff_spans(a: str, b: str) -> tuple[str, str]:
    """Character-level diff between two strings. Returns (a_html, b_html):
    HTML-escaped text with differing runs wrapped in <mark class="diff-del">
    (only in a) / <mark class="diff-add"> (only in b). autojunk=False:
    SequenceMatcher's junk heuristic only engages above ~200 chars, irrelevant
    at field length, disabled defensively anyway."""
    sm = difflib.SequenceMatcher(None, a or "", b or "", autojunk=False)
    a_out, b_out = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        a_chunk, b_chunk = html.escape(a[i1:i2]), html.escape(b[j1:j2])
        if tag == "equal":
            a_out.append(a_chunk)
            b_out.append(b_chunk)
        else:
            if a_chunk:
                a_out.append(f'<mark class="diff-del">{a_chunk}</mark>')
            if b_chunk:
                b_out.append(f'<mark class="diff-add">{b_chunk}</mark>')
    return "".join(a_out), "".join(b_out)


def render_diff_row(label: str, left_val: str, right_val: str,
                     left_col=None, mid_col=None, right_col=None) -> None:
    """Renders one label + two diffed values in a 3-column layout (label /
    left / right). Pass left_col/mid_col/right_col (from a shared st.columns
    call) to align multiple rows into one table-like block; omitted, each row
    gets its own st.columns([1, 3, 3])."""
    if left_col is None:
        left_col, mid_col, right_col = st.columns([1, 3, 3])

    if not left_val.strip() and not right_val.strip():
        with left_col:
            st.caption(label)
        with mid_col:
            st.text("—")
        with right_col:
            st.text("—")
        return

    if left_val.strip().lower() == right_val.strip().lower() and left_val.strip():
        with left_col:
            st.caption(label)
        with mid_col:
            st.markdown(html.escape(left_val))
        with right_col:
            st.markdown("✓ _same_")
        return

    left_html, right_html = char_diff_spans(left_val, right_val)
    with left_col:
        st.caption(label)
    with mid_col:
        st.markdown(left_html or "—", unsafe_allow_html=True)
    with right_col:
        st.markdown(right_html or "—", unsafe_allow_html=True)


def render_diff_block(entity_type: str, left_row: dict, right_row: dict,
                       left_label: str = "Candidate", right_label: str = "Authority match") -> None:
    """Renders the full COMPARE_ROWS field set for entity_type as a labeled
    diff table -- the shared comparison view used by the card queue, New
    Entities' compare panel, and Person/Place Duplicates. Caller is
    responsible for injecting DIFF_CSS once (e.g. near st.set_page_config),
    not per-call."""
    rows = [(label, field_value(left_row, left_keys), field_value(right_row, right_keys))
            for label, left_keys, right_keys in COMPARE_ROWS[entity_type]]
    render_diff_table(rows, left_label, right_label)


def render_diff_table(rows: list, left_label: str = "Left", right_label: str = "Right") -> None:
    """Renders a precomputed list of (label, left_val, right_val) string
    triples as a labeled diff table -- the lower-level primitive
    render_diff_block uses for person/place entities, and what
    review_queue.QueueItem.diff_rows (already-resolved values for the
    non-uniform duplicate-candidate/review-item sources) renders through
    directly."""
    c1, c2, c3 = st.columns([1, 3, 3])
    with c2:
        st.caption(left_label)
    with c3:
        st.caption(right_label)
    for label, left_val, right_val in rows:
        render_diff_row(label, left_val, right_val)
