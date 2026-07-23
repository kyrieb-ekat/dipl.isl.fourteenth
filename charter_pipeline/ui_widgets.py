"""
Shared filter toolbar -- one consistent volume/status/search/sort row reused
everywhere a list of entities is browsed or pre-filtered (the card queue,
New Entities, Person/Place Duplicates, Authority Browser, Charters), instead
of each tab inventing its own ad hoc filter widget.
"""
import pandas as pd
import streamlit as st

DEFAULT_SORT_OPTIONS = ["default", "name"]
SCORE_SORT_OPTIONS = ["default", "score_desc", "score_asc", "name"]


def render_filter_toolbar(session_key: str, volumes: list | None = None,
                           status_options: list | None = None,
                           sort_options: list | None = None,
                           extra: dict | None = None) -> dict:
    """Renders one st.columns row and returns the picked filter values.

    volumes: full list of volume numbers to offer in a multiselect (all
      selected by default); omitted entirely if None (tab is single-volume
      or cross-volume by design).
    status_options: options for a status/confidence selectbox, with an
      implicit leading "(any)" that maps to None; omitted if None.
    sort_options: options for the sort selectbox; defaults to
      DEFAULT_SORT_OPTIONS ("default"/"name" only) -- pass SCORE_SORT_OPTIONS
      for callers whose items carry a meaningful confidence/match score.
    extra: optional {key: (label, options)} for one caller-specific
      selectbox, e.g. Place Duplicates' "include all volumes" toggle.

    Returns {"volumes": [...] | None, "status": str | None, "search": str,
    "sort": str, **extra values keyed by their `extra` dict key}.
    """
    sort_options = sort_options or DEFAULT_SORT_OPTIONS
    extra = extra or {}
    n_cols = bool(volumes) + bool(status_options) + 1 + 1 + len(extra)
    cols = st.columns(n_cols)
    i = 0
    result: dict = {}

    if volumes:
        with cols[i]:
            picked = st.multiselect("Volume", volumes, default=volumes,
                                     format_func=lambda v: f"vol{v:02d}",
                                     key=f"filt_vol_{session_key}")
        result["volumes"] = picked or None
        i += 1
    else:
        result["volumes"] = None

    if status_options:
        with cols[i]:
            picked_status = st.selectbox("Status", ["(any)"] + list(status_options),
                                          key=f"filt_status_{session_key}")
        result["status"] = None if picked_status == "(any)" else picked_status
        i += 1
    else:
        result["status"] = None

    with cols[i]:
        result["search"] = st.text_input("Search", key=f"filt_search_{session_key}")
    i += 1

    with cols[i]:
        result["sort"] = st.selectbox("Sort", sort_options, key=f"filt_sort_{session_key}")
    i += 1

    for key, (label, options) in extra.items():
        with cols[i]:
            result[key] = st.selectbox(label, options, key=f"filt_{key}_{session_key}")
        i += 1

    return result


def filter_dataframe(df, search: str, search_cols: list | None = None,
                      status_col: str | None = None, status: str | None = None):
    """Applies the toolbar's status/search values to a DataFrame -- the
    browse-mode-grid counterpart to review_queue.build_queue's own filtering.
    search_cols: columns to substring-match against; defaults to every
    column (stringified), same behavior the old Authority Browser search had,
    just also usable per-tab with a narrower column set."""
    out = df
    if status_col and status:
        out = out[out[status_col].fillna("") == status]
    if search and search.strip():
        needle = search.strip().lower()
        cols = search_cols or list(out.columns)

        def _row_text(row) -> str:
            # NaN-safe per-value str() -- DataFrame.astype(str) at the
            # frame level doesn't reliably coerce every dtype (observed:
            # pandas left a bare float in place on at least one column/
            # pandas-version combination, crashing " ".join on a non-str
            # item). Checking pd.isna() per value before str() is robust
            # regardless of dtype or pandas version.
            return " ".join("" if pd.isna(v) else str(v) for v in row)

        mask = out[cols].apply(_row_text, axis=1).str.lower().str.contains(
            needle, regex=False)
        out = out[mask]
    return out
