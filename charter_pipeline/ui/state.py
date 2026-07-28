"""Shared per-session UI state and grid helpers.

Extracted verbatim from review_app.py so page modules can share them without
importing the entrypoint (which would re-execute the whole app).

The invariants here were each written in response to a real bug -- read the
individual docstrings before changing any of them. In short: Streamlit
reruns the script on every interaction, and st.data_editor couples its widget
key to the shape of the data it was handed, so anything that changes the
visible row set without also changing the widget key silently drops pending
edits and selections.
"""
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402

# ── generic data/UI helpers ─────────────────────────────────────────────────
#
# No hand-rolled full-dataframe cache: every tab fetches fresh from db.py on
# every render (cheap, indexed SQL reads -- the old CSV-parsing cost that
# justified a persistent cache doesn't apply here). What IS still needed:
# (a) a per-session "last known saved state" snapshot so unsaved in-widget
# edits are never silently discarded/overwritten by a rerun, and (b) a
# widget-key version counter so st.data_editor remounts cleanly after a
# mutation changes the underlying rows out from under it. Both are
# namespaced by the same mergever epoch, so bumping one always invalidates
# the other in lockstep -- eliminates the old app's one confirmed
# cache-invalidation bug (a missed pop() in one call site).


def bump(*keys: str) -> None:
    for k in keys:
        st.session_state[f"_mergever_{k}"] = st.session_state.get(f"_mergever_{k}", 0) + 1


def mergever(key: str) -> int:
    return st.session_state.get(f"_mergever_{key}", 0)


def snap_key(session_key: str) -> str:
    return f"_snap_{session_key}_{mergever(session_key)}"


def ensure_snapshot(session_key: str, df: pd.DataFrame) -> None:
    k = snap_key(session_key)
    if k not in st.session_state:
        st.session_state[k] = df.copy()


def is_dirty(session_key: str, edited: pd.DataFrame) -> bool:
    snap = st.session_state.get(snap_key(session_key))
    return snap is None or not edited.reset_index(drop=True).equals(snap.reset_index(drop=True))


def mark_saved(session_key: str, edited: pd.DataFrame) -> None:
    st.session_state[snap_key(session_key)] = edited.copy()


def reset_snapshot_on_filter_change(session_key: str, filter_sig) -> None:
    """The dirty-check snapshot (ensure_snapshot/is_dirty) assumes the
    underlying row set for session_key only changes when mergever bumps --
    true before the filter toolbar existed, since every tab showed its whole
    dataframe. Now that a filter toolbar can change which rows are visible on
    an otherwise-ordinary rerun (no save, no bump), a stale snapshot from
    before the filter changed would otherwise make is_dirty() spuriously
    report unsaved changes (or worse, silently ignore edits to rows the old
    snapshot never saw). Call this once, right after computing filter_sig and
    before ensure_snapshot, to bump() the moment the filter itself changes."""
    sig_key = f"_filtsig_{session_key}"
    if st.session_state.get(sig_key) != filter_sig:
        st.session_state[sig_key] = filter_sig
        bump(session_key)


def reset_snapshot_on_rowset_change(session_key: str, pks) -> None:
    """Same purpose as reset_snapshot_on_filter_change, but for a different
    trigger: the underlying row set can now also change for a reason
    entirely outside this tab's own bump() calls -- the Review tab mutates
    the exact same underlying tables (persons/places/duplicate-candidates/
    review_queue_items) via its own, separate action codepath, with no
    reason to know this tab's session_key exists. Call this with the FULL
    (pre-filter) set of pks for session_key, once per render, right
    alongside reset_snapshot_on_filter_change -- if the actual row set
    differs from last render for ANY reason, bump immediately, before the
    grid renders, so st.data_editor never gets a stale key paired with
    reshaped data (which is what was silently dropping pending edits/
    selections)."""
    fp = tuple(sorted(pks))
    key = f"_rowset_{session_key}"
    if st.session_state.get(key) != fp:
        st.session_state[key] = fp
        bump(session_key)


def apply_row_diffs(before_df: pd.DataFrame, after_df: pd.DataFrame, id_col: str,
                     update_fn, editable_cols: list[str]) -> int:
    """For every row where any editable_cols value differs between before_df
    and after_df (matched by id_col), calls update_fn(id_value, **changes).
    Returns the number of rows updated."""
    if before_df.empty:
        return 0
    before_by_id = before_df.set_index(id_col)
    n = 0
    for _, row in after_df.iterrows():
        rid = row[id_col]
        if rid not in before_by_id.index:
            continue
        brow = before_by_id.loc[rid]
        changes = {c: row[c] for c in editable_cols
                   if c in brow.index and str(row[c]) != str(brow[c])}
        if changes:
            update_fn(rid, **changes)
            n += 1
    return n


def save_button(session_key: str, edited: pd.DataFrame, apply_fn, label: str = "Save changes") -> None:
    """apply_fn(edited_df) -> int (rows changed): persists pending edits.
    Disabled while there's nothing to save."""
    dirty = is_dirty(session_key, edited)
    col_btn, col_status = st.columns([1, 5])
    with col_btn:
        if st.button(label, key=f"save_{session_key}", disabled=not dirty,
                     type="primary" if dirty else "secondary"):
            n = apply_fn(edited)
            mark_saved(session_key, edited)
            bump(session_key)
            st.toast(f"Saved ({n} row(s) changed).")
            st.rerun()
    with col_status:
        if dirty:
            st.warning("Unsaved changes — click **Save changes** to write them.")
        else:
            st.caption("All changes saved.")


def with_wikidata_links(df: pd.DataFrame, id_col: str = "wikidata_id") -> pd.DataFrame:
    out = df.copy()
    out["wikidata_link"] = out[id_col].fillna("").apply(
        lambda q: f"https://www.wikidata.org/wiki/{q}" if str(q).strip() else ""
    )
    return out


def with_checkbox(df: pd.DataFrame, session_key: str, pk_col: str, col_name: str = "select") -> pd.DataFrame:
    """Seeds the select column from a session-state-backed set of checked pks
    instead of a hardcoded False -- unlike a bare data_editor checkbox column
    (whose checked state lives only in the widget's own ephemeral state), this
    set survives the bump()-triggered widget-key remount every Save button
    causes, so checking rows then saving unrelated edits no longer silently
    clears the selection. Call sync_checked_pks() right after the matching
    st.data_editor call to keep the set in sync with in-grid clicks."""
    checked = st.session_state.setdefault(f"_checked_pks_{session_key}", set())
    out = df.copy()
    out.insert(0, col_name, out[pk_col].isin(checked) if pk_col in out.columns else False)
    return out


def sync_checked_pks(session_key: str, edited_df: pd.DataFrame, pk_col: str, col_name: str = "select") -> None:
    """Call immediately after the st.data_editor call that used with_checkbox,
    before any bump() -- persists whatever's currently checked in the grid
    into the session-state set with_checkbox() reads from on the next render."""
    st.session_state[f"_checked_pks_{session_key}"] = set(
        edited_df.loc[edited_df[col_name] == True, pk_col].tolist())  # noqa: E712


def blank_if_null(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """DataFrame-sourced nullable numeric columns come back as float64 NaN --
    render them as '' in editable text columns rather than the literal
    string 'nan' (same pitfall fixed in person_authority.py/place_authority.py)."""
    out = df.copy()
    for c in cols:
        if c in out.columns:
            out[c] = out[c].apply(lambda v: "" if pd.isna(v) else
                                   (str(int(v)) if float(v).is_integer() else str(v)))
    return out



# ── undo control (rendered on every tab, per plan section 2.5) ─────────────


def undo_widget(location_key: str) -> None:
    last = db.get_last_action()
    if not last:
        return
    confirm_key = f"_undo_confirm_{location_key}"
    col1, col2 = st.columns([1, 5])
    with col1:
        if not st.session_state.get(confirm_key):
            if st.button("↩ Undo", key=f"undo_btn_{location_key}"):
                st.session_state[confirm_key] = True
                st.rerun()
        else:
            if st.button("Confirm undo", key=f"undo_confirm_{location_key}", type="primary"):
                result = db.undo_last_action()
                st.session_state[confirm_key] = False
                st.toast(f"Undone: {result['description']}")
                st.rerun()
    with col2:
        if st.session_state.get(confirm_key):
            st.warning(f"Undo **{last['description']}**? (This is itself only reversible by another undo.)")
        else:
            st.caption(f"Last action: _{last['description']}_")

