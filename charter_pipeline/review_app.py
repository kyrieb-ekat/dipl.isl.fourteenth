"""
DI Charter Authority Review
Run: streamlit run charter_pipeline/review_app.py

Backed by charter_pipeline.db (SQLite) via db.py -- see schema.sql and
migrate_to_sqlite.py for how the old per-volume CSVs + the two independently
-mutable authority stores were unified into one canonical database.
"""
import subprocess
import sys
import threading
import time
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent))
import config
import db
import diff_render
import hotkeys
import review_queue
import ui_widgets

PYTHON = sys.executable
SCRIPTS = Path(__file__).parent

st.set_page_config(page_title="DI Authority Review", layout="wide")
st.markdown(diff_render.DIFF_CSS, unsafe_allow_html=True)


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


# ── pipeline subprocess helpers (unchanged from before the migration) ──────


def run_command(cmd: list[str], session_key: str) -> None:
    rec: dict = {"status": "running", "output": [], "code": None}
    st.session_state[session_key] = rec

    def _worker() -> None:
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, cwd=str(SCRIPTS),
            )
            for line in iter(proc.stdout.readline, ""):
                rec["output"].append(line.rstrip())
            proc.wait()
            rec["code"] = proc.returncode
            rec["status"] = "done" if proc.returncode == 0 else "error"
        except Exception as exc:
            rec["output"].append(f"Launch error: {exc}")
            rec["status"] = "error"
            rec["code"] = -1

    threading.Thread(target=_worker, daemon=True).start()


def step_output(session_key: str) -> bool:
    rec = st.session_state.get(session_key)
    if not rec:
        return False
    lines = rec.get("output", [])
    if lines:
        st.code("\n".join(lines), language=None)
    status = rec.get("status", "idle")
    if status == "done":
        st.success("Done.")
    elif status == "error":
        st.error(f"Exited with code {rec.get('code')}.")
    return status == "running"


def step_label(base: str, *keys: str) -> str:
    statuses = [st.session_state.get(k, {}).get("status", "") for k in keys]
    if "running" in statuses:
        return base + "  (running...)"
    if "error" in statuses:
        return base + "  (error)"
    return base


def is_running(*keys: str) -> bool:
    return any(st.session_state.get(k, {}).get("status") == "running" for k in keys)


def run_sync(cmd: list[str]) -> tuple[int, str]:
    """Run a step and block until it finishes -- used by the chained runner,
    where auto-advancing to the next step only makes sense after this one
    has actually completed (unlike the per-step buttons, which poll async
    so the user can keep an eye on the Pipeline tab while it streams)."""
    proc = subprocess.run(cmd, cwd=str(SCRIPTS), capture_output=True, text=True)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


# ── sidebar ──────────────────────────────────────────────────────────────────

st.title("DI Authority Review")

volumes = db.get_volumes()
if volumes:
    vol_options = [f"vol{v:02d}" for v in volumes]
    vol = st.sidebar.selectbox("Volume", vol_options, index=len(vol_options) - 1)
    vn = int(vol[3:])
else:
    _vn_input = st.sidebar.number_input("Volume number", min_value=1, value=1, step=1)
    vn = int(_vn_input)
    vol = f"vol{vn:02d}"
    st.sidebar.caption("No volumes in the database yet. Run Step 1 in the Pipeline tab to create one.")

st.sidebar.markdown("---")
st.sidebar.caption(f"Database: `{Path(config.DB_PATH).name}`")
st.sidebar.caption(
    "Person Duplicates, Place Duplicates, and Final Review are cross-volume — "
    "the Volume selector above does not filter those tabs."
)

(tab_pipeline, tab_review, tab_charters, tab_queue, tab_entities, tab_dupes,
 tab_place_dupes, tab_final, tab_authority) = st.tabs(
    ["Pipeline", "Review", "Charters", "Review Queue", "New Entities",
     "Person Duplicates", "Place Duplicates", "Final Review", "Authority Browser"]
)


# ── tab: pipeline ────────────────────────────────────────────────────────────

@st.fragment
def render_pipeline_tab():
    # The whole tab body lives inside one fragment so its background-step
    # polling (`if any_running: time.sleep(0.5); st.rerun()` at the bottom)
    # only reruns THIS fragment, not the entire script. Before this, that
    # rerun was a full-page rerun -- and since Streamlit renders every
    # st.tabs() body on every script run regardless of which tab is visually
    # active, a pipeline step (or a stuck "running" status left over from
    # one) would blow away in-progress state on completely different tabs
    # every 0.5s, e.g. resetting a just-checked row in New Entities and
    # collapsing its Compare panel a moment after it appeared.
    undo_widget("pipeline")
    st.caption(
        "Run the whole volume in one go with **Run volume**, or use the individual "
        "steps below for manual control / recovery."
    )

    chain_key = f"chain_{vol}"
    chain = st.session_state.setdefault(chain_key, {"active": False, "idx": 0, "pdf_path": ""})

    CHAIN_STEPS = [
        {"id": "extract_text", "label": "Extract text"},
        {"id": "extract_entities", "label": "Extract entities (Claude API)"},
        {"id": "resolve_entities", "label": "Resolve entities"},
        {"id": "export_to_db", "label": "Export to database"},
        {"id": "pause_triage", "label": "Triage new data"},
        {"id": "geocode", "label": "Geocode places"},
        {"id": "nafnid", "label": "Nafnid reconciliation"},
        {"id": "pause_places", "label": "Set place Status"},
        {"id": "pause_flags", "label": "Final flagged-charter check"},
        {"id": "export_authority", "label": "Export authority XLSX"},
    ]

    with st.container(border=True):
        st.markdown("**Run volume**")
        if not chain["active"]:
            chain["pdf_path"] = st.text_input(
                "PDF path (only needed if Step 1 hasn't been run for this volume yet)",
                value=chain.get("pdf_path", ""),
                placeholder=str(config.PDF_DIR / f"Diplomatarium_Islandicum___Bindi_{vn}.pdf"),
                key="chain_pdf_input",
            )
            if st.button("▶ Run volume", key="chain_start", type="primary"):
                chain["active"] = True
                chain["idx"] = 0
                st.rerun()
        else:
            idx = chain["idx"]
            for i, step in enumerate(CHAIN_STEPS):
                icon = "✓" if i < idx else ("▶" if i == idx else "⏳")
                st.markdown(f"{icon} {step['label']}")
                if i == idx:
                    break

            if idx >= len(CHAIN_STEPS):
                st.success("Chain complete for this volume.")
                if st.button("Reset", key="chain_reset"):
                    chain["active"] = False
                    chain["idx"] = 0
                    st.rerun()
            else:
                cur = CHAIN_STEPS[idx]

                if cur["id"] == "extract_text":
                    pdf = chain.get("pdf_path", "").strip()
                    if not pdf:
                        st.warning("No PDF path given — skipping text extraction "
                                   "(assuming segments already exist for this volume).")
                        chain["idx"] += 1
                        st.rerun()
                    else:
                        code, out = run_sync([PYTHON, "01_extract_text.py", "--pdf", pdf, "--vol", str(vn)])
                        st.code(out[-3000:], language=None)
                        if code == 0:
                            chain["idx"] += 1
                            st.rerun()
                        else:
                            st.error(f"Step 1 failed (exit {code}). Fix the PDF path and click Run volume again.")
                            chain["active"] = False

                elif cur["id"] == "extract_entities":
                    st.warning(
                        "This calls the Claude API once per charter and may take several "
                        "minutes and consume API credits."
                    )
                    if st.button("Start extraction", key="chain_confirm_s2", type="primary"):
                        code, out = run_sync([PYTHON, "02_extract_entities.py", "--vol", str(vn)])
                        st.code(out[-3000:], language=None)
                        if code == 0:
                            chain["idx"] += 1
                            st.rerun()
                        else:
                            st.error(f"Step 2 failed (exit {code}).")
                            chain["active"] = False
                    if st.button("Cancel chain", key="chain_cancel_s2"):
                        chain["active"] = False
                        st.rerun()

                elif cur["id"] == "resolve_entities":
                    code, out = run_sync([PYTHON, "03_resolve_entities.py", "--vol", str(vn)])
                    st.code(out[-3000:], language=None)
                    if code == 0:
                        chain["idx"] += 1
                        st.rerun()
                    else:
                        st.error(f"Step 3 failed (exit {code}).")
                        chain["active"] = False

                elif cur["id"] == "export_to_db":
                    code, out = run_sync([PYTHON, "05_export_csvs.py", "--vol", str(vn), "--force"])
                    st.code(out[-3000:], language=None)
                    if code == 0:
                        chain["idx"] += 1
                        st.rerun()
                    else:
                        st.error(f"Step 5 failed (exit {code}).")
                        chain["active"] = False

                elif cur["id"] == "pause_triage":
                    n_open = len(db.get_open_review_items(vn))
                    n_new_p = len(db.get_persons(status="provisional", source_volume=vn))
                    n_new_pl = len(db.get_places(status="provisional", source_volume=vn))
                    n_flagged = len(db.get_charters(volume=vn, has_review=True))
                    st.info(
                        f"**{n_open}** open Review Queue item(s) · **{n_new_p}** new person(s) · "
                        f"**{n_new_pl}** new place(s) · **{n_flagged}** flagged charter(s). "
                        "Nothing downstream requires these at zero — this is a checkpoint, not a hard gate."
                    )
                    if st.button("Continue pipeline", key="chain_continue_triage", type="primary"):
                        chain["idx"] += 1
                        st.rerun()

                elif cur["id"] == "geocode":
                    code, out = run_sync([PYTHON, "04_lookup_coords.py", "--vol", str(vn)])
                    st.code(out[-3000:], language=None)
                    if code == 0:
                        chain["idx"] += 1
                        st.rerun()
                    else:
                        st.error(f"Step 4 failed (exit {code}). This usually means a network/SPARQL "
                                 "endpoint problem -- check the output above and retry.")
                        chain["active"] = False

                elif cur["id"] == "nafnid":
                    code, out = run_sync([PYTHON, "04a_reconcile_nafnid.py", "--vol", str(vn)])
                    st.code(out[-3000:], language=None)
                    if code == 0:
                        chain["idx"] += 1
                        st.rerun()
                    else:
                        st.error(f"Step 4a failed (exit {code}).")
                        chain["active"] = False

                elif cur["id"] == "pause_places":
                    places_df = db.get_places(status="provisional", source_volume=vn)
                    counts = places_df["review_status"].fillna("").value_counts() if not places_df.empty else {}
                    n_ok = counts.get("ok", 0); n_skip = counts.get("skip", 0)
                    n_add = counts.get("add", 0); n_blank = counts.get("", 0)
                    st.info(
                        f"Places Status breakdown: **ok**={n_ok} · **skip**={n_skip} · "
                        f"**add**={n_add} · **blank**={n_blank}. "
                        "Blank rows are exported like **ok** by default — mark **skip** in "
                        "New Entities > Places to exclude a row, **add** to also promote it."
                    )
                    if st.button("Continue pipeline", key="chain_continue_places", type="primary"):
                        chain["idx"] += 1
                        st.rerun()

                elif cur["id"] == "pause_flags":
                    db.rescan_review_flags(vn)
                    n_flagged = len(db.get_charters(volume=vn, has_review=True))
                    if n_flagged:
                        st.warning(
                            f"**{n_flagged}** charter(s) still flagged (parse error / unresolved "
                            "persons or places). See the **Charters** tab (filtered to flagged) "
                            "and the **Review Queue** tab."
                        )
                        c1, c2 = st.columns(2)
                        with c1:
                            if st.button("Re-check now", key="chain_recheck_flags"):
                                st.rerun()
                        with c2:
                            if st.button(f"Continue anyway ({n_flagged} will be skipped by export)",
                                          key="chain_continue_anyway"):
                                chain["idx"] += 1
                                st.rerun()
                    else:
                        st.success("No flagged charters remain.")
                        chain["idx"] += 1
                        st.rerun()

                elif cur["id"] == "export_authority":
                    code, out = run_sync([PYTHON, "06_export_authority.py", "--vol", str(vn), "--dry-run"])
                    st.code(out, language=None)
                    if st.button("Apply export", key="chain_apply_export", type="primary"):
                        code2, out2 = run_sync([PYTHON, "06_export_authority.py", "--vol", str(vn)])
                        st.code(out2, language=None)
                        if code2 == 0:
                            chain["idx"] += 1
                            st.rerun()
                    if st.button("Cancel chain", key="chain_cancel_export"):
                        chain["active"] = False
                        st.rerun()

    st.markdown("---")
    st.caption("Individual steps (manual control / recovery):")
    any_running = False

    with st.expander(step_label("One-time setup — seed authority CSVs from XLSX", "setup_pl", "setup_pe")):
        col_pl, col_pe = st.columns(2)
        with col_pl:
            overwrite_pl = st.checkbox("--overwrite", key="setup_pl_ow")
            cmd_pl = [PYTHON, "seed_place_names.py"] + (["--overwrite"] if overwrite_pl else [])
            if st.button("Seed place authority", key="btn_setup_pl", disabled=is_running("setup_pl")):
                run_command(cmd_pl, "setup_pl"); st.rerun()
            if step_output("setup_pl"):
                any_running = True
        with col_pe:
            overwrite_pe = st.checkbox("--overwrite", key="setup_pe_ow")
            cmd_pe = [PYTHON, "seed_person_names.py"] + (["--overwrite"] if overwrite_pe else [])
            if st.button("Seed person authority", key="btn_setup_pe", disabled=is_running("setup_pe")):
                run_command(cmd_pe, "setup_pe"); st.rerun()
            if step_output("setup_pe"):
                any_running = True

    s1_key = f"s1_{vol}"
    with st.expander(step_label("Step 1 — Extract charter text from PDF", s1_key)):
        pdf_input = st.text_input("PDF path", key="s1_pdf",
                                    placeholder=str(config.PDF_DIR / f"Diplomatarium_Islandicum___Bindi_{vn}.pdf"))
        if st.button("Run", key="btn_s1", disabled=is_running(s1_key) or not pdf_input.strip()):
            run_command([PYTHON, "01_extract_text.py", "--pdf", pdf_input.strip(), "--vol", str(vn)], s1_key)
            st.rerun()
        if step_output(s1_key):
            any_running = True

    s2_key = f"s2_{vol}"
    with st.expander(step_label("Step 2 — Extract entities with Claude API", s2_key)):
        use_batch = st.checkbox("Use batch range", key="s2_batch")
        cmd2 = [PYTHON, "02_extract_entities.py", "--vol", str(vn)]
        if use_batch:
            col_s, col_e = st.columns(2)
            with col_s:
                s2_start = st.number_input("Start charter", min_value=1, value=1, key="s2_start")
            with col_e:
                s2_end = st.number_input("End charter", min_value=1, value=50, key="s2_end")
            cmd2 += ["--start", str(int(s2_start)), "--end", str(int(s2_end))]
        if st.button("Run", key="btn_s2", disabled=is_running(s2_key)):
            run_command(cmd2, s2_key); st.rerun()
        if step_output(s2_key):
            any_running = True

    s3_key = f"s3_{vol}"
    with st.expander(step_label("Step 3 — Resolve entities against canonical persons/places", s3_key)):
        col_fa, col_fr = st.columns(2)
        with col_fa:
            s3_accept = st.slider("Auto-assign threshold", 1, 100, 85, key="s3_accept")
        with col_fr:
            s3_review = st.slider("Review threshold", 1, 100, 60, key="s3_review")
        if s3_review >= s3_accept:
            st.warning("Review threshold must be lower than the auto-assign threshold.")
        cmd3 = [PYTHON, "03_resolve_entities.py", "--vol", str(vn),
                "--fuzzy-accept", str(s3_accept), "--fuzzy-review", str(s3_review)]
        if st.button("Run", key="btn_s3", disabled=is_running(s3_key) or s3_review >= s3_accept):
            run_command(cmd3, s3_key); st.rerun()
        if step_output(s3_key):
            any_running = True

    s5_key = f"s5_{vol}"
    with st.expander(step_label("Step 5 — Export into the database", s5_key)):
        st.caption("Loads charters/charter_persons/charter_places into the DB. Not idempotent "
                   "by design -- pass --force to replace an already-loaded volume.")
        force5 = st.checkbox("--force (replace existing)", key="s5_force")
        cmd5 = [PYTHON, "05_export_csvs.py", "--vol", str(vn)] + (["--force"] if force5 else [])
        if st.button("Run", key="btn_s5", disabled=is_running(s5_key)):
            run_command(cmd5, s5_key); st.rerun()
        if step_output(s5_key):
            any_running = True

    s4_key = f"s4_{vol}"
    with st.expander(step_label("Step 4 — Geocode new places via Wikidata", s4_key)):
        if st.button("Run", key="btn_s4", disabled=is_running(s4_key)):
            run_command([PYTHON, "04_lookup_coords.py", "--vol", str(vn)], s4_key); st.rerun()
        if step_output(s4_key):
            any_running = True

    s4a_key = f"s4a_{vol}"
    with st.expander(step_label("Step 4a — Reconcile against nafnid.is", s4a_key)):
        st.caption("Writes candidates to the **Place Duplicates** tab for manual triage.")
        if st.button("Run", key="btn_s4a", disabled=is_running(s4a_key)):
            run_command([PYTHON, "04a_reconcile_nafnid.py", "--vol", str(vn)], s4a_key); st.rerun()
        if step_output(s4a_key):
            any_running = True

    s6_key = f"s6_{vol}"
    with st.expander(step_label("Step 6 — Export authority XLSX + nodegoat CSV", s6_key)):
        col_dr, col_ap = st.columns(2)
        with col_dr:
            if st.button("Dry run", key="btn_s6_dr", disabled=is_running(f"{s6_key}_dr")):
                run_command([PYTHON, "06_export_authority.py", "--vol", str(vn), "--dry-run"], f"{s6_key}_dr")
                st.rerun()
            if step_output(f"{s6_key}_dr"):
                any_running = True
        with col_ap:
            if st.button("Apply", key="btn_s6", disabled=is_running(s6_key)):
                run_command([PYTHON, "06_export_authority.py", "--vol", str(vn)], s6_key)
                st.rerun()
            if step_output(s6_key):
                any_running = True

    if any_running:
        time.sleep(0.5)
        st.rerun()


with tab_pipeline:
    render_pipeline_tab()


# ── tab: review (unified card queue) ────────────────────────────────────────
#
# Primary review workflow: surfaces every pending decision -- new-entity
# curation, person/place duplicate candidates, review-queue items -- as one
# card at a time, with a shared char-level diff (diff_render.py) and hotkey
# labels. The 8 tabs below this one remain as browse/bulk-edit/audit
# surfaces; this tab is purely additive on top of them (plan section 5).

_QUEUE_TYPE_LABELS = {
    "new_person": "New person", "new_place": "New place",
    "person_dup": "Person duplicate", "place_dup": "Place duplicate",
    "review_item": "Review queue item",
}

with tab_review:
    undo_widget("review")
    st.caption(
        "Works through every pending decision -- new entities, person/place "
        "duplicate candidates, and review-queue items -- one at a time."
    )

    fcol1, fcol2 = st.columns([2, 3])
    with fcol1:
        rq_volumes = st.multiselect(
            "Volumes (blank = all)", volumes, default=[],
            format_func=lambda v: f"vol{v:02d}", key="rq_volumes")
    with fcol2:
        rq_types = st.multiselect(
            "Item types", list(review_queue.ALL_ITEM_TYPES),
            default=list(review_queue.ALL_ITEM_TYPES),
            format_func=lambda t: _QUEUE_TYPE_LABELS[t], key="rq_types")
    rq_tb = ui_widgets.render_filter_toolbar(
        "review_queue", sort_options=ui_widgets.SCORE_SORT_OPTIONS)

    rq_filt = review_queue.QueueFilter(
        volumes=rq_volumes or None,
        item_types=set(rq_types) if rq_types else set(review_queue.ALL_ITEM_TYPES),
        search=rq_tb["search"], sort=rq_tb["sort"],
    )

    rq_filt_sig = (tuple(sorted(rq_filt.volumes or [])), tuple(sorted(rq_filt.item_types)),
                   rq_filt.search, rq_filt.sort)
    if st.session_state.get("_queue_filter_sig") != rq_filt_sig:
        st.session_state["_queue_filter_sig"] = rq_filt_sig
        st.session_state["_queue_pos"] = 0
        st.session_state["_queue_prefetch"] = None

    @st.fragment
    def render_review_queue_fragment(rq_filt):
        # Scoped to its own fragment so an action click's st.rerun() doesn't
        # force every OTHER tab's (possibly expensive) grids to re-render too
        # -- Streamlit's st.tabs() renders every tab's body in the DOM
        # regardless of which is visible, same fact behind the earlier
        # hotkey-leak fix. Filter widgets stay outside (above), since they
        # need a normal full-script rerun on change and must run before this.
        rq_index = review_queue.build_queue_index(rq_filt)
        rq_pos = st.session_state.get("_queue_pos", 0)

        if not rq_index:
            st.success("Queue complete for this filter. 🎉")
            return

        rq_pos = max(0, min(rq_pos, len(rq_index) - 1))
        st.session_state["_queue_pos"] = rq_pos
        rq_entry = rq_index[rq_pos]

        # Reuse the previous render's prefetch if it's still the same entry
        # (the common case: either "next" just advanced onto exactly what
        # was prefetched, or nothing happened and we're re-showing the same
        # item) -- avoids re-paying materialize()'s search_authority cost.
        prefetch = st.session_state.get("_queue_prefetch")
        if prefetch and prefetch.get("item_id") == rq_entry.item_id:
            rq_item = prefetch["item"]
        else:
            rq_item = review_queue.materialize(rq_entry)

        st.progress((rq_pos + 1) / len(rq_index))
        st.caption(f"**{rq_pos + 1} / {len(rq_index)}** in queue — "
                   f"{_QUEUE_TYPE_LABELS[rq_item.item_type]}"
                   + (f" · vol{rq_item.volume:02d}" if rq_item.volume else ""))

        with st.container(border=True):
            st.markdown(f"#### {rq_item.header}")
            st.caption(rq_item.subheader)
            diff_render.render_diff_table(rq_item.diff_rows, rq_item.left_label, rq_item.right_label)

            btn_cols = st.columns(len(rq_item.actions))
            # Widget key is the action name ALONE (stable across items/reruns),
            # not item_id -- item_id contains a literal ":" (e.g.
            # "new_place:157"), invalid inside the CSS class selector
            # hotkeys.bind_hotkeys builds from it. One bind_hotkeys() call per
            # render (below), not one per button, keeps this to a single
            # extra invisible iframe -- with one iframe per button (an
            # earlier attempt using the third-party streamlit-shortcuts
            # package), keyboard focus intermittently got captured by one of
            # the many 0-height iframes and real keydown events then never
            # reached the page-level listener at all.
            action_hotkeys = {}
            for col, rq_action in zip(btn_cols, rq_item.actions):
                with col:
                    clicked = st.button(
                        rq_action.label, key=f"rq_act_{rq_action.action}",
                        type="primary" if rq_action.style == "primary" else "secondary",
                    )
                    action_hotkeys[f"rq_act_{rq_action.action}"] = rq_action.hotkey
                    if clicked:
                        if rq_action.action == "next":
                            # This item stays in the live queue (nothing was
                            # decided) -- explicitly advance position, unlike
                            # every other action, where the acted-on item
                            # drops out of build_queue_index()'s result on its
                            # own and the same index naturally lands on the
                            # next item for free. Prefetch survives (a
                            # "next" is a pure no-op, nothing it could have
                            # invalidated), so the item about to be shown is
                            # already materialized.
                            st.session_state["_queue_pos"] = rq_pos + 1
                        else:
                            review_queue.apply_action(rq_item, rq_action.action)
                            st.toast(f"{rq_action.label.split(' (')[0]} — done.")
                            # Unlike "next", a real action can change data the
                            # prefetched next item's own materialization
                            # depended on (e.g. a merge changing the
                            # authority table) -- don't trust it, recompute
                            # fresh next render.
                            st.session_state["_queue_prefetch"] = None
                        st.rerun()
            hotkeys.bind_hotkeys(action_hotkeys)

        # Prefetch the item one position ahead now, while nothing has been
        # clicked yet -- by the time the user does click, the following
        # render's materialize() cost is already paid.
        if rq_pos + 1 < len(rq_index):
            next_entry = rq_index[rq_pos + 1]
            if not (prefetch and prefetch.get("item_id") == next_entry.item_id):
                st.session_state["_queue_prefetch"] = {
                    "item_id": next_entry.item_id,
                    "item": review_queue.materialize(next_entry),
                }
        else:
            st.session_state["_queue_prefetch"] = None

    render_review_queue_fragment(rq_filt)


# ── tab: charters ────────────────────────────────────────────────────────────

with tab_charters:
    undo_widget("charters")
    flagged_only = st.checkbox("Flagged only", value=True, key="charters_flagged_only")
    ckey = f"{vol}_charters"

    df_c_all = db.get_charters(volume=vn, has_review=(True if flagged_only else None))
    if df_c_all.empty:
        st.info("No charters" + (" match this filter" if flagged_only else "") + " for this volume.")
    else:
        tb_c = ui_widgets.render_filter_toolbar(ckey)
        df_c = ui_widgets.filter_dataframe(
            df_c_all, search=tb_c["search"],
            search_cols=["doc_type", "subject", "outcome", "scribe", "di_reference"])
        if tb_c["sort"] == "name":
            df_c = df_c.sort_values("sequence")
        if df_c.empty:
            st.info("No rows match this filter.")
        else:
            def _status_badge(r):
                parts = []
                if r["has_parse_error"]:
                    parts.append("Parse error")
                if r["has_review_persons"]:
                    parts.append("Unresolved persons")
                if r["has_review_places"]:
                    parts.append("Unresolved places")
                return " + ".join(parts) if parts else "OK"

            df_c = df_c.copy()
            df_c["Status"] = df_c.apply(_status_badge, axis=1)

            editable_cols = ["date", "date_uncertain", "doc_type", "subject", "outcome",
                              "scribe", "scribe_source", "seal_info", "language"]
            readonly_cols = ["charter_pk", "sequence", "di_reference", "Status", "notes"]
            reset_snapshot_on_filter_change(
                ckey, (flagged_only, tb_c["search"], tb_c["sort"]))
            # Snapshot the SAME column subset handed to the editor below -- snapshotting
            # the full fetched frame here would make is_dirty() always report dirty
            # (column-set mismatch always fails .equals()).
            ensure_snapshot(ckey, df_c[readonly_cols + editable_cols])

            mv = mergever(ckey)
            edited_c = st.data_editor(
                df_c[readonly_cols + editable_cols],
                key=f"ed_{ckey}_{mv}",
                use_container_width=True, num_rows="fixed", hide_index=True,
                column_config={
                    "charter_pk": None,
                    "sequence": st.column_config.NumberColumn("Seq", width="small", disabled=True),
                    "di_reference": st.column_config.TextColumn("DI ref.", width="medium", disabled=True),
                    "Status": st.column_config.TextColumn("Status", width="medium", disabled=True),
                    "notes": st.column_config.TextColumn("Notes (diagnostic)", width="large", disabled=True),
                    "date": st.column_config.TextColumn("Date", width="small"),
                    "date_uncertain": st.column_config.TextColumn("Uncertain?", width="small"),
                    "doc_type": st.column_config.TextColumn("Doc type", width="small"),
                    "subject": st.column_config.TextColumn("Subject", width="medium"),
                    "outcome": st.column_config.TextColumn("Outcome", width="medium"),
                    "scribe": st.column_config.TextColumn("Scribe", width="small"),
                    "scribe_source": st.column_config.TextColumn("Scribe source", width="small"),
                    "seal_info": st.column_config.TextColumn("Seal info", width="small"),
                    "language": st.column_config.TextColumn("Language", width="small"),
                },
            )

            def _apply_charters(edited_df):
                def _update(pk, **changes):
                    row = df_c.loc[df_c["charter_pk"] == pk].iloc[0]
                    db.update_charter(int(row["volume"]), int(row["sequence"]), **changes)
                return apply_row_diffs(st.session_state[snap_key(ckey)], edited_df, "charter_pk",
                                        _update, editable_cols)

            dirty_c = is_dirty(ckey, edited_c)
            save_button(ckey, edited_c, _apply_charters)

            st.markdown("---")
            st.caption("Row actions for still-flagged charters:")
            for _, row in df_c[df_c["Status"] != "OK"].iterrows():
                with st.container(border=True):
                    st.markdown(f"**Seq {row['sequence']}** ({row['di_reference'] or 'no DI ref.'}) — {row['Status']}")
                    b1, b2 = st.columns(2)
                    with b1:
                        if row["has_review_persons"] or row["has_review_places"]:
                            st.caption("→ Resolve the remaining item(s) in the **Review Queue** tab.")
                    with b2:
                        if row["has_parse_error"]:
                            if st.button(f"Re-extract seq {row['sequence']}", key=f"reextract_{row['charter_pk']}"):
                                run_command(
                                    [PYTHON, "02_extract_entities.py", "--vol", str(vn),
                                     "--start", str(row["sequence"]), "--end", str(row["sequence"])],
                                    f"reextract_{row['charter_pk']}",
                                )
                                st.info("Re-extraction started — re-run Step 3 and Step 5 in the "
                                        "Pipeline tab afterward to bring it into the database.")


# ── tab: review queue ────────────────────────────────────────────────────────

with tab_queue:
    undo_widget("queue")
    sub_pending, sub_resolved = st.tabs(["Pending", "Resolved"])

    with sub_pending:
        qkey = f"{vol}_queue"
        df_q_all = db.get_open_review_items(vn)
        if df_q_all.empty:
            st.info("No open review queue items for this volume.")
        else:
            tb_q = ui_widgets.render_filter_toolbar(qkey, status_options=["", "accept", "reject"])
            df_q = ui_widgets.filter_dataframe(
                df_q_all, search=tb_q["search"],
                search_cols=["extracted_name", "closest_match", "role_category", "role"],
                status_col="decision", status=tb_q["status"])
            if tb_q["sort"] == "name":
                df_q = df_q.sort_values("extracted_name")
            if df_q.empty:
                st.info("No rows match this filter.")
            else:
                reset_snapshot_on_filter_change(qkey, (tb_q["status"], tb_q["search"], tb_q["sort"]))
                ensure_snapshot(qkey, df_q)
                mv = mergever(qkey)
                edited_q = st.data_editor(
                    with_checkbox(df_q, qkey, "review_item_pk"),
                    key=f"ed_{qkey}_{mv}",
                    use_container_width=True, num_rows="fixed", hide_index=True,
                    column_order=["select", "review_item_pk", "entity_type", "extracted_name",
                                  "closest_match", "match_pk", "score", "role_category", "role", "decision"],
                    column_config={
                        "select": st.column_config.CheckboxColumn("Select", width="small"),
                        "review_item_pk": None,
                        "charter_pk": None, "charter_person_pk": None, "charter_place_pk": None,
                        "outcome_pk": None, "status": None, "resolved_at": None,
                        "created_at": None, "charter_date": None, "charter_volume": None,
                        "entity_type": st.column_config.TextColumn("Type", width="small", disabled=True),
                        "extracted_name": st.column_config.TextColumn("Extracted name", width="medium", disabled=True),
                        "closest_match": st.column_config.TextColumn("Closest match", width="medium", disabled=True),
                        "match_pk": st.column_config.NumberColumn("Proposed pk", width="small", disabled=True),
                        "score": st.column_config.NumberColumn("Score", format="%.1f", width="small", disabled=True),
                        "role_category": st.column_config.TextColumn("Role", width="small", disabled=True),
                        "role": st.column_config.TextColumn("Place role", width="small", disabled=True),
                        "decision": st.column_config.SelectboxColumn(
                            "Decision", options=["", "accept", "reject"], width="small",
                            help="accept = use proposed pk  ·  reject = treat as new entity",
                        ),
                    },
                )
                sync_checked_pks(qkey, edited_q, "review_item_pk")

                n_done = (edited_q["decision"].fillna("").str.strip() != "").sum()
                st.caption(f"**{n_done} / {len(edited_q)}** decisions recorded")

                checked_pks_q = edited_q.loc[edited_q["select"] == True, "review_item_pk"].tolist()  # noqa: E712
                col_pick, col_bulk, col_status = st.columns([1, 1, 4])
                dirty_q = is_dirty(qkey, edited_q.drop(columns=["select"]))
                with col_pick:
                    bulk_val = st.selectbox("Bulk decision", ["accept", "reject"],
                                              key=f"bulkval_{qkey}", label_visibility="collapsed")
                with col_bulk:
                    if st.button("Apply to selected", key=f"btn_bulk_{qkey}",
                                 disabled=dirty_q or len(checked_pks_q) < 2):
                        for pk in checked_pks_q:
                            db.set_review_decision(int(pk), bulk_val)
                        bump(qkey)
                        st.toast(f"Set decision='{bulk_val}' on {len(checked_pks_q)} row(s).")
                        st.rerun()
                with col_status:
                    if dirty_q:
                        st.caption("Save your pending changes below before bulk-applying.")
                    elif len(checked_pks_q) >= 2:
                        st.caption(f"{len(checked_pks_q)} rows checked.")

                def _apply_queue(edited_df):
                    # edited_df here is already the post-drop frame save_button()
                    # was called with below -- dropping "select" again would KeyError.
                    def _update(pk, **changes):
                        db.set_review_decision(pk, changes["decision"])
                    return apply_row_diffs(st.session_state[snap_key(qkey)],
                                            edited_df, "review_item_pk", _update, ["decision"])
                save_button(qkey, edited_q.drop(columns=["select"]), _apply_queue)

                st.markdown("---")
                if st.button("Resolve decided rows", key=f"btn_resolve_{qkey}", type="primary",
                             disabled=dirty_q):
                    accepted = rejected = 0
                    for _, row in edited_q.iterrows():
                        if (row["decision"] or "").strip().lower() in ("accept", "reject"):
                            result = db.apply_review_decision(int(row["review_item_pk"]))
                            if result.get("decision") == "accept":
                                accepted += 1
                            elif result.get("decision") == "reject":
                                rejected += 1
                    bump(qkey, f"{vol}_persons", f"{vol}_places")
                    st.toast(f"Accepted {accepted}, rejected {rejected}.")
                    st.rerun()

    with sub_resolved:
        df_r = db.get_resolved_review_items(vn)
        if df_r.empty:
            st.info("No rows resolved yet for this volume.")
        else:
            st.caption(f"**{len(df_r)}** row(s) resolved — read-only archive.")
            st.dataframe(
                df_r[["entity_type", "extracted_name", "closest_match", "decision", "outcome_pk", "resolved_at"]],
                use_container_width=True, hide_index=True,
            )


# ── tab: new entities ────────────────────────────────────────────────────────

with tab_entities:
    undo_widget("entities")
    sub_p, sub_pl = st.tabs(["Persons", "Places"])

    with sub_p:
        pkey = f"{vol}_persons"
        st.caption("New-vs-authority comparison and ok/add/skip decisions now happen in the "
                   "**Review** tab. This grid is for bulk field edits and merging duplicate rows.")
        df_p_all = db.get_persons(status="provisional", source_volume=vn)
        if df_p_all.empty:
            st.info("No new persons for this volume.")
        else:
            tb_p = ui_widgets.render_filter_toolbar(pkey, status_options=["", "ok", "skip", "add"])
            df_p = ui_widgets.filter_dataframe(
                df_p_all, search=tb_p["search"],
                search_cols=["canonical_name", "variant_names", "occupation", "title", "notes"],
                status_col="review_status", status=tb_p["status"])
            if tb_p["sort"] == "name":
                df_p = df_p.sort_values("canonical_name")
            if df_p.empty:
                st.info("No rows match this filter.")
            else:
                reset_snapshot_on_filter_change(pkey, (tb_p["status"], tb_p["search"], tb_p["sort"]))
                df_p = blank_if_null(df_p, ["floruit_start", "floruit_end"])
                ensure_snapshot(pkey, df_p)
                mv = mergever(pkey)

                editable_p = ["review_status", "canonical_name", "wikidata_id", "variant_names",
                              "patronymic", "occupation", "title", "floruit_start", "floruit_end",
                              "gender", "associated_places", "notes"]

                edited_p = st.data_editor(
                    with_checkbox(with_wikidata_links(df_p), pkey, "person_pk"),
                    key=f"ed_{pkey}_{mv}",
                    use_container_width=True, num_rows="fixed", hide_index=True,
                    column_order=["select", "person_pk", "display_id", "review_status", "canonical_name",
                                  "wikidata_id", "wikidata_link", "variant_names", "patronymic",
                                  "occupation", "title", "floruit_start", "floruit_end", "gender",
                                  "associated_places", "notes", "sources"],
                    column_config={
                        "select": st.column_config.CheckboxColumn(
                            "Select", width="small", help="Check 2+ rows to merge them."),
                        "person_pk": None,
                        "display_id": st.column_config.TextColumn("ID", width="small", disabled=True),
                        "review_status": st.column_config.SelectboxColumn(
                            "Status", options=["", "ok", "skip", "add"], width="small"),
                        "canonical_name": st.column_config.TextColumn("Canonical name", width="medium"),
                        "wikidata_id": st.column_config.TextColumn("Wikidata ID", width="small"),
                        "wikidata_link": st.column_config.LinkColumn("Wikidata", width="small", disabled=True),
                        "variant_names": st.column_config.TextColumn("Variants", width="large"),
                        "patronymic": st.column_config.TextColumn("Patronymic", width="small"),
                        "occupation": st.column_config.TextColumn("Occupation", width="medium"),
                        "title": st.column_config.TextColumn("Title", width="small"),
                        "floruit_start": st.column_config.TextColumn("Fl. start", width="small"),
                        "floruit_end": st.column_config.TextColumn("Fl. end", width="small"),
                        "gender": st.column_config.SelectboxColumn("Gender", options=["", "M", "F", "unknown"], width="small"),
                        "associated_places": st.column_config.TextColumn("Places", width="medium"),
                        "notes": st.column_config.TextColumn("Notes", width="large"),
                        "sources": st.column_config.TextColumn("Sources", width="small", disabled=True),
                    },
                )
                sync_checked_pks(pkey, edited_p, "person_pk")

                counts = edited_p["review_status"].fillna("").value_counts()
                st.caption(f"ok: {counts.get('ok', 0)} · skip: {counts.get('skip', 0)} · "
                           f"add: {counts.get('add', 0)} · blank: {counts.get('', 0)} "
                           "— blank behaves like **ok** at export time.")

                checked_p = edited_p.loc[edited_p["select"] == True, "person_pk"].tolist()  # noqa: E712
                dirty_p = is_dirty(pkey, edited_p.drop(columns=["select", "wikidata_link"]))

                c1, c2 = st.columns([1, 4])
                with c1:
                    if st.button("Merge selected", key="btn_merge_p",
                                 disabled=dirty_p or len(checked_p) < 2):
                        survivor = min(checked_p)
                        dropped = [pk for pk in checked_p if pk != survivor]
                        result = db.merge_persons(survivor, dropped)
                        bump(pkey)
                        st.toast(f"Merged {len(dropped)} row(s) into person_pk={survivor}.")
                        st.rerun()
                with c2:
                    if dirty_p:
                        st.caption("Save pending changes before merging.")
                    elif len(checked_p) >= 2:
                        st.caption(f"{len(checked_p)} rows selected — will merge into the lowest pk.")

                def _apply_persons(edited_df):
                    # edited_df is already the post-drop frame save_button() was
                    # called with below -- dropping select/wikidata_link again
                    # would KeyError since they're already gone.
                    def _update(pk, **changes):
                        if "floruit_start" in changes:
                            changes["floruit_start"] = db.to_int_or_none(changes["floruit_start"])
                        if "floruit_end" in changes:
                            changes["floruit_end"] = db.to_int_or_none(changes["floruit_end"])
                        db.update_person(pk, **changes)
                    return apply_row_diffs(st.session_state[snap_key(pkey)],
                                            edited_df, "person_pk", _update, editable_p)
                save_button(pkey, edited_p.drop(columns=["select", "wikidata_link"]), _apply_persons)

    with sub_pl:
        plkey = f"{vol}_places"
        st.caption("New-vs-authority comparison and ok/add/skip/no_match decisions now happen "
                   "in the **Review** tab. This grid is for bulk field edits and merging duplicate rows.")
        df_pl_all = db.get_places(status="provisional", source_volume=vn)
        if df_pl_all.empty:
            st.info("No new places for this volume.")
        else:
            tb_pl = ui_widgets.render_filter_toolbar(plkey, status_options=["", "ok", "skip", "add", "no_match"])
            df_pl = ui_widgets.filter_dataframe(
                df_pl_all, search=tb_pl["search"],
                search_cols=["canonical_name", "variant_names", "region", "notes"],
                status_col="review_status", status=tb_pl["status"])
            if tb_pl["sort"] == "name":
                df_pl = df_pl.sort_values("canonical_name")
            if df_pl.empty:
                st.info("No rows match this filter.")
            else:
                reset_snapshot_on_filter_change(plkey, (tb_pl["status"], tb_pl["search"], tb_pl["sort"]))
                df_pl = blank_if_null(df_pl, ["coordinates_lat", "coordinates_long", "geo_match_score"])
                ensure_snapshot(plkey, df_pl)
                mv = mergever(plkey)

                editable_pl = ["review_status", "canonical_name", "wikidata_id", "nafnid_id",
                               "variant_names", "place_type", "coordinates_lat", "coordinates_long",
                               "region", "district", "modern_equivalent", "notes"]

                edited_pl = st.data_editor(
                    with_checkbox(with_wikidata_links(df_pl), plkey, "place_pk"),
                    key=f"ed_{plkey}_{mv}",
                    use_container_width=True, num_rows="fixed", hide_index=True,
                    column_order=["select", "place_pk", "display_id", "review_status", "canonical_name",
                                  "wikidata_id", "wikidata_link", "nafnid_id", "variant_names", "place_type",
                                  "coordinates_lat", "coordinates_long", "region", "district",
                                  "modern_equivalent", "notes", "sources"],
                    column_config={
                        "select": st.column_config.CheckboxColumn(
                            "Select", width="small", help="Check 2+ rows to merge them."),
                        "place_pk": None,
                        "display_id": st.column_config.TextColumn("ID", width="small", disabled=True),
                        "review_status": st.column_config.SelectboxColumn(
                            "Status", options=["", "ok", "skip", "add", "no_match"], width="small"),
                        "canonical_name": st.column_config.TextColumn("Canonical name", width="medium"),
                        "wikidata_id": st.column_config.TextColumn("Wikidata ID", width="small"),
                        "wikidata_link": st.column_config.LinkColumn("Wikidata", width="small", disabled=True),
                        "nafnid_id": st.column_config.TextColumn("nafnid ID", width="small"),
                        "variant_names": st.column_config.TextColumn("Variants", width="large"),
                        "place_type": st.column_config.TextColumn("Type", width="small"),
                        "coordinates_lat": st.column_config.TextColumn("Lat", width="small"),
                        "coordinates_long": st.column_config.TextColumn("Lon", width="small"),
                        "region": st.column_config.TextColumn("Region", width="small"),
                        "district": st.column_config.TextColumn("District", width="small"),
                        "modern_equivalent": st.column_config.TextColumn("Modern equiv.", width="medium"),
                        "notes": st.column_config.TextColumn("Notes", width="large"),
                        "sources": st.column_config.TextColumn("Sources", width="small", disabled=True),
                    },
                )
                sync_checked_pks(plkey, edited_pl, "place_pk")

                counts_pl = edited_pl["review_status"].fillna("").value_counts()
                st.caption(f"ok: {counts_pl.get('ok', 0)} · skip: {counts_pl.get('skip', 0)} · "
                           f"add: {counts_pl.get('add', 0)} · blank: {counts_pl.get('', 0)} "
                           "— blank behaves like **ok** at export time.")

                checked_pl = edited_pl.loc[edited_pl["select"] == True, "place_pk"].tolist()  # noqa: E712
                dirty_pl = is_dirty(plkey, edited_pl.drop(columns=["select", "wikidata_link"]))

                c1, c2 = st.columns([1, 4])
                with c1:
                    if st.button("Merge selected", key="btn_merge_pl",
                                 disabled=dirty_pl or len(checked_pl) < 2):
                        survivor = min(checked_pl)
                        dropped = [pk for pk in checked_pl if pk != survivor]
                        result = db.merge_places(survivor, dropped)
                        bump(plkey)
                        st.toast(f"Merged {len(dropped)} row(s) into place_pk={survivor}.")
                        st.rerun()
                with c2:
                    if dirty_pl:
                        st.caption("Save pending changes before merging.")
                    elif len(checked_pl) >= 2:
                        st.caption(f"{len(checked_pl)} rows selected — will merge into the lowest pk.")

                def _apply_places(edited_df):
                    # edited_df is already the post-drop frame save_button() was
                    # called with below -- dropping select/wikidata_link again
                    # would KeyError since they're already gone.
                    def _update(pk, **changes):
                        for f in ("coordinates_lat", "coordinates_long"):
                            if f in changes:
                                try:
                                    changes[f] = float(changes[f]) if str(changes[f]).strip() else None
                                except ValueError:
                                    changes[f] = None
                        name_changed = "canonical_name" in changes or "variant_names" in changes
                        db.update_place(pk, **changes)
                        if name_changed:
                            db.reconcile_place_wikidata(pk)
                    return apply_row_diffs(st.session_state[snap_key(plkey)],
                                            edited_df, "place_pk", _update, editable_pl)
                save_button(plkey, edited_pl.drop(columns=["select", "wikidata_link"]), _apply_places)


# ── tab: person duplicates ─────────────────────────────────────────────────

with tab_dupes:
    undo_widget("person_dupes")
    st.caption(
        "Compares every provisional person across all volumes against each other and "
        "against the canonical authority. Flag-only: marking a decision here never "
        "modifies any charter reference on its own."
    )

    @st.fragment
    def render_duplicate_finder_control():
        # Scoped to its own fragment for the same reason as the Pipeline tab
        # (see render_pipeline_tab's comment): its poll loop must not trigger
        # a full-page rerun that would reset unrelated state elsewhere.
        dup_run_key = "s7_dupes"
        if st.button("Run duplicate finder", key="btn_s7", disabled=is_running(dup_run_key)):
            run_command([PYTHON, "07_find_person_duplicates.py"], dup_run_key)
            st.rerun()
        if step_output(dup_run_key):
            time.sleep(0.5)
            st.rerun()

    render_duplicate_finder_control()
    st.caption("Comparing candidates one at a time, with a character-level diff, now also "
               "happens in the **Review** tab. This grid is for bulk browsing/filtering.")

    dkey = "person_dupes"
    df_dupes_all = db.get_person_duplicate_candidates()
    if df_dupes_all.empty:
        st.info("No duplicate candidates on file yet. Click **Run duplicate finder** above.")
    else:
        tb_d = ui_widgets.render_filter_toolbar(
            dkey, status_options=["", "same", "different"], sort_options=ui_widgets.SCORE_SORT_OPTIONS)
        df_dupes = ui_widgets.filter_dataframe(
            df_dupes_all, search=tb_d["search"], status_col="decision", status=tb_d["status"])
        if tb_d["sort"] == "score_desc":
            df_dupes = df_dupes.sort_values("name_score", ascending=False)
        elif tb_d["sort"] == "score_asc":
            df_dupes = df_dupes.sort_values("name_score", ascending=True)
        elif tb_d["sort"] == "name":
            df_dupes = df_dupes.sort_values("a_canonical_name")

        if df_dupes.empty:
            st.info("No rows match this filter.")
        else:
            reset_snapshot_on_filter_change(dkey, (tb_d["status"], tb_d["search"], tb_d["sort"]))
            ensure_snapshot(dkey, df_dupes)
            mv = mergever(dkey)
            edited_dupes = st.data_editor(
                with_checkbox(df_dupes, dkey, "candidate_pk"),
                key=f"ed_{dkey}_{mv}",
                use_container_width=True, num_rows="fixed", hide_index=True,
                column_order=["select", "a_display_id", "a_canonical_name", "a_source", "a_floruit_start",
                              "a_floruit_end", "a_occupation", "a_title", "b_display_id", "b_canonical_name",
                              "b_source", "b_floruit_start", "b_floruit_end", "b_occupation", "b_title",
                              "name_score", "date_status", "classification", "confidence", "decision"],
                column_config={
                    "select": st.column_config.CheckboxColumn("Select", width="small"),
                    "candidate_pk": None, "person_a_pk": None, "person_b_pk": None, "decided_at": None, "created_at": None,
                    "a_display_id": st.column_config.TextColumn("A · ID", width="small", disabled=True),
                    "a_canonical_name": st.column_config.TextColumn("A · Name", width="medium", disabled=True),
                    "a_source": st.column_config.TextColumn("A · Source", width="small", disabled=True),
                    "a_floruit_start": st.column_config.NumberColumn("A · Fl. start", width="small", disabled=True),
                    "a_floruit_end": st.column_config.NumberColumn("A · Fl. end", width="small", disabled=True),
                    "a_occupation": st.column_config.TextColumn("A · Occupation", width="medium", disabled=True),
                    "a_title": st.column_config.TextColumn("A · Title", width="medium", disabled=True),
                    "b_display_id": st.column_config.TextColumn("B · ID", width="small", disabled=True),
                    "b_canonical_name": st.column_config.TextColumn("B · Name", width="medium", disabled=True),
                    "b_source": st.column_config.TextColumn("B · Source", width="small", disabled=True),
                    "b_floruit_start": st.column_config.NumberColumn("B · Fl. start", width="small", disabled=True),
                    "b_floruit_end": st.column_config.NumberColumn("B · Fl. end", width="small", disabled=True),
                    "b_occupation": st.column_config.TextColumn("B · Occupation", width="medium", disabled=True),
                    "b_title": st.column_config.TextColumn("B · Title", width="medium", disabled=True),
                    "name_score": st.column_config.NumberColumn("Name score", format="%.0f", width="small", disabled=True),
                    "date_status": st.column_config.TextColumn("Dates", width="small", disabled=True),
                    "classification": st.column_config.TextColumn("Classification", width="medium", disabled=True),
                    "confidence": st.column_config.TextColumn("Confidence", width="small", disabled=True),
                    "decision": st.column_config.SelectboxColumn(
                        "Decision", options=["", "same", "different"], width="small"),
                },
            )
            sync_checked_pks(dkey, edited_dupes, "candidate_pk")

            n_done = (edited_dupes["decision"].fillna("").str.strip() != "").sum()
            st.caption(f"**{n_done} / {len(edited_dupes)}** decisions recorded — sorted highest-confidence first")

            def _apply_dupes(edited_df):
                # edited_df is already the post-drop frame save_button() was
                # called with below -- dropping "select" again would KeyError.
                def _update(pk, **changes):
                    db.record_person_duplicate_decision(pk, changes["decision"])
                return apply_row_diffs(st.session_state[snap_key(dkey)],
                                        edited_df, "candidate_pk", _update, ["decision"])
            dirty_d = is_dirty(dkey, edited_dupes.drop(columns=["select"]))
            save_button(dkey, edited_dupes.drop(columns=["select"]), _apply_dupes)

            checked_d = edited_dupes[edited_dupes["select"] == True]  # noqa: E712
            st.markdown("---")
            col_send, col_status = st.columns([1, 5])
            with col_send:
                if st.button("Send to Final Review", key="btn_send_final", disabled=dirty_d or checked_d.empty):
                    sent = skipped_not_different = 0
                    for _, row in checked_d.iterrows():
                        if (row["decision"] or "").strip().lower() != "different":
                            skipped_not_different += 1
                            continue
                        for pk in (row["person_a_pk"], row["person_b_pk"]):
                            p = db.get_person_by_pk(int(pk))
                            if p and p["status"] == "provisional":
                                db.update_person(int(pk), review_status="add")
                                sent += 1
                    st.toast(f"Marked {sent} person(s) review_status=add. "
                             f"Skipped {skipped_not_different} not yet 'different'.")
                    st.rerun()
            with col_status:
                if not checked_d.empty:
                    st.caption(f"{len(checked_d)} row(s) checked — only rows marked "
                               "**different** are sent; provisional sides get review_status=add.")


# ── tab: place duplicates ────────────────────────────────────────────────────

with tab_place_dupes:
    undo_widget("place_dupes")
    st.caption(
        "Candidates from nafnid.is (Árnastofnun) reconciliation (Step 4a). No confirmed-'same' "
        "signal exists yet, so these are always warnings in Final Review, never a hard block. "
        "Comparing one at a time now also happens in the **Review** tab."
    )

    pdkey = "place_dupes"
    df_pdupes_all = db.get_place_duplicate_candidates(volume=None)
    if df_pdupes_all.empty:
        st.info("No place duplicate candidates on file. Run Step 4a in the Pipeline tab.")
    else:
        tb_pd = ui_widgets.render_filter_toolbar(
            pdkey, volumes=volumes, status_options=["", "same", "different"],
            sort_options=ui_widgets.SCORE_SORT_OPTIONS)
        df_pdupes = ui_widgets.filter_dataframe(
            df_pdupes_all, search=tb_pd["search"], status_col="decision", status=tb_pd["status"])
        if tb_pd["volumes"]:
            df_pdupes = df_pdupes[df_pdupes["source_volume"].isin(tb_pd["volumes"])]
        if tb_pd["sort"] == "score_desc":
            df_pdupes = df_pdupes.sort_values("name_score", ascending=False)
        elif tb_pd["sort"] == "score_asc":
            df_pdupes = df_pdupes.sort_values("name_score", ascending=True)
        elif tb_pd["sort"] == "name":
            df_pdupes = df_pdupes.sort_values("place_canonical_name")

        if df_pdupes.empty:
            st.info("No rows match this filter.")
        else:
            reset_snapshot_on_filter_change(
                pdkey, (tuple(sorted(tb_pd["volumes"] or [])), tb_pd["status"], tb_pd["search"], tb_pd["sort"]))
            ensure_snapshot(pdkey, df_pdupes)
            mv = mergever(pdkey)
            edited_pdupes = st.data_editor(
                with_checkbox(df_pdupes, pdkey, "candidate_pk"),
                key=f"ed_{pdkey}_{mv}",
                use_container_width=True, num_rows="fixed", hide_index=True,
                column_order=["select", "display_id", "place_canonical_name", "source_volume", "di_name",
                              "candidate_name", "candidate_rank", "name_score", "distance_km", "flag",
                              "match_sources", "candidate_sysla", "decision"],
                column_config={
                    "select": st.column_config.CheckboxColumn("Select", width="small"),
                    "candidate_pk": None, "place_pk": None, "di_sysla_given": None, "di_place_type": None,
                    "di_region": None, "wikidata_status": None, "candidate_nafnid": None,
                    "candidate_hreppur": None, "candidate_lat": None, "candidate_lng": None, "created_at": None,
                    "display_id": st.column_config.TextColumn("Place ID", width="small", disabled=True),
                    "place_canonical_name": st.column_config.TextColumn("DI place", width="medium", disabled=True),
                    "source_volume": st.column_config.NumberColumn("Vol", width="small", disabled=True),
                    "di_name": st.column_config.TextColumn("DI name", width="medium", disabled=True),
                    "candidate_name": st.column_config.TextColumn("nafnid candidate", width="medium", disabled=True),
                    "candidate_rank": st.column_config.NumberColumn("Rank", width="small", disabled=True),
                    "name_score": st.column_config.NumberColumn("Name score", format="%.1f", width="small", disabled=True),
                    "distance_km": st.column_config.NumberColumn("Dist. (km)", format="%.1f", width="small", disabled=True),
                    "flag": st.column_config.TextColumn("Flag", width="small", disabled=True),
                    "match_sources": st.column_config.TextColumn("Sources", width="small", disabled=True),
                    "candidate_sysla": st.column_config.TextColumn("Sýsla", width="small", disabled=True),
                    "decision": st.column_config.SelectboxColumn(
                        "Decision", options=["", "same", "different"], width="small",
                        help="same = confirmed match, backfills nafnid_id  ·  different = false positive"),
                },
            )
            sync_checked_pks(pdkey, edited_pdupes, "candidate_pk")

            n_done = (edited_pdupes["decision"].fillna("").str.strip() != "").sum()
            st.caption(f"**{n_done} / {len(edited_pdupes)}** decisions recorded — sorted highest-confidence first")

            def _apply_pdupes(edited_df):
                # edited_df is already the post-drop frame save_button() was
                # called with below -- dropping "select" again would KeyError.
                def _update(pk, **changes):
                    db.record_place_duplicate_decision(pk, changes["decision"])
                return apply_row_diffs(st.session_state[snap_key(pdkey)],
                                        edited_df, "candidate_pk", _update, ["decision"])
            save_button(pdkey, edited_pdupes.drop(columns=["select"]), _apply_pdupes)


# ── tab: final review ────────────────────────────────────────────────────────

with tab_final:
    undo_widget("final")
    all_volumes = db.get_volumes()
    rows = db.get_final_review_candidates(all_volumes) if all_volumes else []

    n_persons = sum(1 for r in rows if r["entity_type"] == "person")
    n_places = sum(1 for r in rows if r["entity_type"] == "place")
    n_blocked = sum(1 for r in rows if r["duplicate_status"] == "blocked")
    n_warning = sum(1 for r in rows if r["duplicate_status"] == "warning")

    st.caption(
        f"**{n_persons}** person(s), **{n_places}** place(s) ready to promote across "
        f"**{len(all_volumes)}** volume(s) — **{n_blocked}** blocked, **{n_warning}** with warnings."
    )

    def _status_label(status: str) -> str:
        return {"blocked": "BLOCKED — confirmed duplicate", "warning": "⚠ Unresolved duplicate"}.get(status, "Ready")

    sub_final_p, sub_final_pl = st.tabs(["Persons", "Places"])

    with sub_final_p:
        person_rows = [r for r in rows if r["entity_type"] == "person"]
        if not person_rows:
            st.info("No persons currently marked review_status=add.")
        else:
            df_final_p = pd.DataFrame(person_rows)
            df_final_p["Status"] = df_final_p["duplicate_status"].apply(_status_label)
            for _, r in df_final_p.iterrows():
                with st.container(border=True):
                    st.markdown(f"**{r['id']}** — {r['canonical_name']}  (vol{r['volume']:02d})  ·  {r['Status']}")
                    if r["duplicate_detail"]:
                        st.caption(r["duplicate_detail"] +
                                   " — resolve this in the **Person Duplicates** tab or the **Review** tab.")

    with sub_final_pl:
        place_rows = [r for r in rows if r["entity_type"] == "place"]
        if not place_rows:
            st.info("No places currently marked review_status=add.")
        else:
            df_final_pl = pd.DataFrame(place_rows)
            df_final_pl["Status"] = df_final_pl["duplicate_status"].apply(_status_label)
            for _, r in df_final_pl.iterrows():
                with st.container(border=True):
                    st.markdown(f"**{r['id']}** — {r['canonical_name']}  (vol{r['volume']:02d})  ·  {r['Status']}")
                    if r["duplicate_detail"]:
                        st.caption(r["duplicate_detail"] +
                                   " — resolve this in the **Place Duplicates** tab or the **Review** tab.")

    if n_blocked:
        st.caption("Rows marked BLOCKED will not be promoted until the duplicate is resolved.")

    n_eligible = sum(1 for r in rows if r["duplicate_status"] != "blocked")
    if st.button("Add to authority file", key="btn_final_promote", type="primary", disabled=n_eligible == 0):
        result = db.promote_all(all_volumes)
        p, pl = result["persons"], result["places"]
        st.toast(
            f"Added {len(p['added'])} person(s), {len(pl['added'])} place(s) to the authority. "
            f"Skipped {len(p['skipped_blocked'])} blocked, "
            f"{len(p['skipped_existing']) + len(pl['skipped_existing'])} already present."
        )
        st.rerun()


# ── tab: authority browser ─────────────────────────────────────────────────

with tab_authority:
    tb_auth = ui_widgets.render_filter_toolbar("authority")
    auth_pl_tab, auth_pe_tab = st.tabs(["Places", "Persons"])

    with auth_pl_tab:
        auth_pl = db.get_places(status="canonical")
        auth_pl = with_wikidata_links(auth_pl)
        auth_pl = ui_widgets.filter_dataframe(auth_pl, search=tb_auth["search"])
        if tb_auth["sort"] == "name":
            auth_pl = auth_pl.sort_values("canonical_name")
        st.caption(f"{len(auth_pl)} entries")
        st.dataframe(
            auth_pl.drop(columns=["created_at", "updated_at"], errors="ignore"),
            use_container_width=True, hide_index=True,
            column_config={"wikidata_link": st.column_config.LinkColumn("Wikidata")},
        )

    with auth_pe_tab:
        auth_pe = db.get_persons(status="canonical")
        auth_pe = with_wikidata_links(auth_pe)
        auth_pe = ui_widgets.filter_dataframe(auth_pe, search=tb_auth["search"])
        if tb_auth["sort"] == "name":
            auth_pe = auth_pe.sort_values("canonical_name")
        st.caption(f"{len(auth_pe)} entries")
        st.dataframe(
            auth_pe.drop(columns=["created_at", "updated_at"], errors="ignore"),
            use_container_width=True, hide_index=True,
            column_config={"wikidata_link": st.column_config.LinkColumn("Wikidata")},
        )
