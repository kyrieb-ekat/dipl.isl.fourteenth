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
import review_queue
import ui.dashboard as ui_dashboard
import ui_widgets
from ui.cards import render_item_card, sanitized_key  # noqa: F401

PYTHON = sys.executable
SCRIPTS = Path(__file__).parent

st.set_page_config(page_title="DI Authority Review", layout="wide")
st.markdown(diff_render.DIFF_CSS, unsafe_allow_html=True)

# Fail loudly on a wrong/empty database rather than rendering an app that
# looks fine and shows nothing -- sqlite3.connect() creates an empty file for
# a path that doesn't exist, so this is a real and already-observed failure.
_db_problem = db.check_database()
if _db_problem:
    st.error(f"**Database problem.** {_db_problem}")
    st.caption("Fix the path, then reload. Nothing has been read or written.")
    st.stop()


# Shared per-session UI state + grid helpers now live in ui/state.py so page
# modules can use them without importing this entrypoint. Imported by name
# rather than as a module so the existing call sites below read unchanged.
from ui.state import (  # noqa: E402
    apply_row_diffs, blank_if_null, bump, ensure_snapshot, is_dirty, mark_saved,
    mergever, reset_snapshot_on_filter_change, reset_snapshot_on_rowset_change,
    save_button, snap_key, sync_checked_pks, undo_widget, with_checkbox,
    with_wikidata_links,
)


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
# db.DB_PATH, not config.DB_PATH: db.py is what every query actually opens,
# so this caption should report the database in use rather than a second,
# independently-resolved copy of the same setting.
st.sidebar.caption(f"Database: `{Path(db.DB_PATH).name}`")
st.sidebar.caption(
    "Person Duplicates, Place Duplicates, and Final Review are cross-volume — "
    "the Volume selector above does not filter those tabs."
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


def page_pipeline():
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


def page_review():
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
    rq_flagged_only = st.checkbox(
        "Data-quality flagged persons only", value=False, key="rq_flagged_only",
        help="Surfaces persons.data_quality_flag rows (set by "
             "09_flag_transmission_actors.py) across all volumes, regardless of "
             "status/review_status -- narrows to just New person items while checked.")

    rq_filt = review_queue.QueueFilter(
        volumes=rq_volumes or None,
        item_types={"new_person"} if rq_flagged_only
                    else (set(rq_types) if rq_types else set(review_queue.ALL_ITEM_TYPES)),
        search=rq_tb["search"], sort=rq_tb["sort"], flagged_only=rq_flagged_only,
    )

    rq_filt_sig = (tuple(sorted(rq_filt.volumes or [])), tuple(sorted(rq_filt.item_types)),
                   rq_filt.search, rq_filt.sort, rq_filt.flagged_only)
    if st.session_state.get("_queue_filter_sig") != rq_filt_sig:
        st.session_state["_queue_filter_sig"] = rq_filt_sig
        st.session_state["_queue_pos"] = 0
        st.session_state["_queue_prefetch"] = None

    rq_mode = st.radio(
        "View", ["Single card", "List + detail"], key="rq_mode", horizontal=True,
        help="Single card: fast, keyboard-driven, one decision at a time. "
             "List + detail: browse many at once (e.g. sorted by name, to spot "
             "look-alike spellings that are probably the same place/person), "
             "then optionally merge several selected ones together.")

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

        def _advance(action_key):
            if action_key == "next":
                # This item stays in the live queue (nothing was decided) --
                # explicitly advance position, unlike every other action,
                # where the acted-on item drops out of build_queue_index()'s
                # result on its own and the same index naturally lands on
                # the next item for free. Prefetch survives (a "next" is a
                # pure no-op, nothing it could have invalidated), so the
                # item about to be shown is already materialized.
                st.session_state["_queue_pos"] = rq_pos + 1
            else:
                # Unlike "next", a real action can change data the
                # prefetched next item's own materialization depended on
                # (e.g. a merge changing the authority table) -- don't trust
                # it, recompute fresh next render.
                st.session_state["_queue_prefetch"] = None

        render_item_card(rq_item, on_action=_advance, key_prefix="rq_card")

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

    @st.fragment
    def render_review_queue_list_fragment(rq_filt):
        # Second, separate fragment (not shared with the single-card mode's)
        # so each mode's rerun cost stays isolated from the other's.
        rq_index = review_queue.build_queue_index(rq_filt)

        if not rq_index:
            st.success("Queue complete for this filter. 🎉")
            return

        # Force a clean widget remount whenever the underlying row-identity
        # set changes for ANY reason -- an action taken here, OR the
        # single-card mode / another tab mutating the same data -- same
        # fix as reset_snapshot_on_rowset_change elsewhere in this file,
        # applied to st.dataframe's own selection state instead of a
        # data_editor snapshot. Without this, a stable widget key paired
        # with reshaped data risks stale/wrong row positions being reported
        # as "selected" (every real action here always removes the acted-on
        # entry from the next build_queue_index() call, so this alone is
        # enough to reset selection after any action -- no separate
        # on_action callback needed for the single-selected-item card below).
        row_fp = tuple(sorted(e.item_id for e in rq_index))
        if st.session_state.get("_rq_list_rowset") != row_fp:
            st.session_state["_rq_list_rowset"] = row_fp
            bump("rq_list")
        list_key = f"rq_list_{mergever('rq_list')}"

        list_df = pd.DataFrame([
            {"Name": e.list_label, "Type": _QUEUE_TYPE_LABELS[e.item_type],
             "Score": round(e.sort_score, 1) if e.sort_score else None,
             "Vol": f"vol{e.volume:02d}" if e.volume else ""}
            for e in rq_index
        ])
        st.caption(f"**{len(rq_index)}** items in queue for this filter. "
                   "Select one to preview, or several to merge them together.")
        result = st.dataframe(
            list_df, hide_index=True, use_container_width=True,
            on_select="rerun", selection_mode="multi-row", key=list_key,
            height=min(400, 78 + 35 * len(rq_index)),
        )
        selected_positions = result["selection"]["rows"]

        if not selected_positions:
            st.info("Select a row above to preview it here.")
            return

        selected_entries = [rq_index[i] for i in selected_positions]

        if len(selected_entries) == 1:
            rq_item = review_queue.materialize(selected_entries[0])
            render_item_card(rq_item, key_prefix="rq_listdetail")
            return

        # 2+ selected: lightweight summary only, no materialize() calls.
        st.write(f"**{len(selected_entries)} items selected:**")
        for e in selected_entries:
            st.caption(f"- {e.list_label}")

        item_types = {e.item_type for e in selected_entries}
        if len(item_types) > 1:
            st.warning(f"Selected items are a mix of types "
                       f"({', '.join(_QUEUE_TYPE_LABELS[t] for t in sorted(item_types))}) -- "
                       "select entries of only one type to merge them.")
            return
        item_type = selected_entries[0].item_type
        if item_type not in review_queue.MERGEABLE_ITEM_TYPES:
            st.warning(f"{_QUEUE_TYPE_LABELS[item_type]} items can't be merged with each other.")
            return

        if st.button(f"Merge {len(selected_entries)} selected", type="primary", key="rq_multi_merge"):
            merge_result = review_queue.apply_multi_merge(selected_entries)
            st.toast(f"Merged {len(selected_entries)} item(s) into survivor pk={merge_result['survivor_pk']}.")
            st.rerun()

    if rq_mode == "Single card":
        render_review_queue_fragment(rq_filt)
    else:
        render_review_queue_list_fragment(rq_filt)


# ── tab: charters ────────────────────────────────────────────────────────────

def page_charters():
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
            reset_snapshot_on_rowset_change(ckey, df_c_all["charter_pk"])
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

def page_queue():
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
                reset_snapshot_on_rowset_change(qkey, df_q_all["review_item_pk"])
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

def page_entities():
    undo_widget("entities")
    sub_p, sub_pl = st.tabs(["Persons", "Places"])

    with sub_p:
        pkey = f"{vol}_persons"
        st.caption("New-vs-authority comparison and ok/add/skip decisions now happen in the "
                   "**Review** tab. This grid is for bulk field edits and merging duplicate rows.")
        flagged_only_p = st.checkbox(
            "Data-quality flagged only (ignores volume/status below)", value=False, key=f"{pkey}_flagged_only",
            help="Rows 09_flag_transmission_actors.py suspects are actually later "
                 "manuscript-transmission actors (copyists/editors/annotators), not "
                 "period-contemporary persons -- shown across all volumes and regardless "
                 "of status/review_status, since a flagged row can already be canonical "
                 "or reviewed.")
        df_p_all = db.get_persons(flagged_only=True) if flagged_only_p \
            else db.get_persons(status="provisional", source_volume=vn)
        if df_p_all.empty:
            st.info("No flagged persons." if flagged_only_p else "No new persons for this volume.")
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
                reset_snapshot_on_rowset_change(pkey, df_p_all["person_pk"])
                df_p = blank_if_null(df_p, ["floruit_start", "floruit_end"])
                ensure_snapshot(pkey, df_p)
                mv = mergever(pkey)

                editable_p = ["review_status", "canonical_name", "wikidata_id", "variant_names",
                              "patronymic", "occupation", "title", "floruit_start", "floruit_end",
                              "gender", "associated_places", "notes", "data_quality_flag"]

                edited_p = st.data_editor(
                    with_checkbox(with_wikidata_links(df_p), pkey, "person_pk"),
                    key=f"ed_{pkey}_{mv}",
                    use_container_width=True, num_rows="fixed", hide_index=True,
                    column_order=["select", "person_pk", "display_id", "data_quality_flag", "review_status",
                                  "canonical_name", "wikidata_id", "wikidata_link", "variant_names",
                                  "patronymic", "occupation", "title", "floruit_start", "floruit_end", "gender",
                                  "associated_places", "notes", "sources"],
                    column_config={
                        "select": st.column_config.CheckboxColumn(
                            "Select", width="small", help="Check 2+ rows to merge them."),
                        "person_pk": None,
                        "display_id": st.column_config.TextColumn("ID", width="small", disabled=True),
                        "data_quality_flag": st.column_config.TextColumn(
                            "Flag", width="small",
                            help="Set by 09_flag_transmission_actors.py. Clear it (blank) once you've "
                                 "confirmed this row -- whether it's a real later-transmission actor to "
                                 "fix/delete, or a false positive (e.g. a genuine period-contemporary person "
                                 "who happens to share a name with someone from a different era)."),
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
                reset_snapshot_on_rowset_change(plkey, df_pl_all["place_pk"])
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

def page_person_dupes():
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
            reset_snapshot_on_rowset_change(dkey, df_dupes_all["candidate_pk"])
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

def page_place_dupes():
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
            reset_snapshot_on_rowset_change(pdkey, df_pdupes_all["candidate_pk"])
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

def page_final():
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

def page_authority():
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

# ── page: browse (one table at a time) ───────────────────────────────────────
#
# The five grid screens were five sibling tabs, named after the SQL tables they
# read rather than after any task. Grouped here behind a single selector so
# exactly ONE renders per run -- st.tabs() would put all five in the DOM
# simultaneously, which is the cost this whole restructure exists to remove.

BROWSE_TABLES = {
    "New entities": page_entities,
    "Review queue": page_queue,
    "Person duplicates": page_person_dupes,
    "Place duplicates": page_place_dupes,
    "Authority file": page_authority,
}


def page_browse():
    st.caption(
        "Bulk field edits, sorting and search across the raw tables. The "
        "one-decision-at-a-time flow lives on the **Review** page."
    )
    choice = st.radio("Table", list(BROWSE_TABLES), horizontal=True,
                      key="_browse_table", label_visibility="collapsed")
    st.markdown("---")
    BROWSE_TABLES[choice]()


def page_dashboard():
    ui_dashboard.render(goto=_goto)


# ── navigation ───────────────────────────────────────────────────────────────
#
# st.navigation instead of st.tabs: only the selected page's function runs, so
# a click no longer pays to re-render every other screen. Measured before this
# change: 17 tab panels and 99 buttons in the DOM at once, 74 of them
# invisible. That render-everything behaviour was the direct cause of three
# separate fixed bugs (~20s Review-tab latency, the hotkey leak firing buttons
# on hidden tabs, and cross-tab loss of grid selections), and it is also why
# hotkeys.py needs an offsetParent visibility guard at all.
#
# Pages are callables rather than separate script files so the page bodies
# could move here unchanged; a `pages/` DIRECTORY would additionally trigger
# Streamlit's own automatic navigation and compete with this registry.

# Placeholder for the Phase 2/4 role split: guests will be propose-only and
# must not reach Pipeline (it shells out to the numbered scripts and spends
# Anthropic API credits) or Promote (promotion is ratification).
IS_OWNER = True

_PAGE_KEYS = {
    # No url_path: Streamlit serves the DEFAULT page at "/", so declaring one
    # advertises a /dashboard link that resolves to "Page not found".
    "dashboard": st.Page(page_dashboard, title="Dashboard", icon=":material/dashboard:",
                         default=True),
    "review": st.Page(page_review, title="Review", icon=":material/fact_check:",
                      url_path="review"),
    "charters": st.Page(page_charters, title="Charters", icon=":material/description:",
                        url_path="charters"),
    "browse": st.Page(page_browse, title="Browse", icon=":material/table_rows:",
                      url_path="browse"),
    "promote": st.Page(page_final, title="Promote", icon=":material/publish:",
                       url_path="promote"),
    "pipeline": st.Page(page_pipeline, title="Pipeline", icon=":material/terminal:",
                        url_path="pipeline"),
}


def _goto(page_key: str) -> None:
    """Used by the Dashboard's start-here button."""
    st.switch_page(_PAGE_KEYS[page_key])


_sections = {"Work": [_PAGE_KEYS["dashboard"], _PAGE_KEYS["review"],
                      _PAGE_KEYS["charters"], _PAGE_KEYS["browse"]]}
if IS_OWNER:
    _sections["Owner"] = [_PAGE_KEYS["promote"], _PAGE_KEYS["pipeline"]]

st.navigation(_sections).run()
