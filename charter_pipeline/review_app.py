"""
DI Charter Authority Review
Run: streamlit run charter_pipeline/review_app.py
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

PYTHON = sys.executable
SCRIPTS = Path(__file__).parent

st.set_page_config(page_title="DI Authority Review", layout="wide")


# ── data helpers ───────────────────────────────────────────────────────────────


def load_csv(path: Path, add_cols: dict | None = None) -> pd.DataFrame:
    """Read CSV as strings; insert any missing columns with their default values."""
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path, dtype=str, on_bad_lines="warn").fillna("")
    if add_cols:
        for col, default in add_cols.items():
            if col not in df.columns:
                df.insert(len(df.columns), col, default)
    return df


def autosave(session_key: str, edited: pd.DataFrame, path: Path) -> None:
    """Write to disk whenever the edited df differs from the session-state snapshot."""
    prev = st.session_state.get(f"_snap_{session_key}")
    if prev is None or not edited.equals(prev):
        edited.to_csv(path, index=False)
        st.session_state[f"_snap_{session_key}"] = edited.copy()
        st.toast("Saved")


def with_wikidata_links(df: pd.DataFrame, id_col: str = "wikidata_id") -> pd.DataFrame:
    out = df.copy()
    out["wikidata_link"] = out[id_col].apply(
        lambda q: f"https://www.wikidata.org/wiki/{q}" if q.strip() else ""
    )
    return out


def available_volumes() -> list[str]:
    if not config.REVIEW_DIR.exists():
        return []
    return sorted(
        p.stem.replace("_review_queue", "")
        for p in config.REVIEW_DIR.glob("*_review_queue.csv")
    )


# ── pipeline helpers ───────────────────────────────────────────────────────────


def run_command(cmd: list[str], session_key: str) -> None:
    """Launch cmd in a background thread; stream stdout/stderr into session_state."""
    rec: dict = {"status": "running", "output": [], "code": None}
    st.session_state[session_key] = rec

    def _worker() -> None:
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=str(SCRIPTS),
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
    """Render step output. Returns True if the step is still running."""
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
    """Append a status suffix to an expander label based on session state."""
    statuses = [st.session_state.get(k, {}).get("status", "") for k in keys]
    if "running" in statuses:
        return base + "  (running...)"
    if "error" in statuses:
        return base + "  (error)"
    return base


def is_running(*keys: str) -> bool:
    return any(st.session_state.get(k, {}).get("status") == "running" for k in keys)


def vol_num(v: str) -> int:
    """'vol04' -> 4"""
    return int(v[3:])


# ── sidebar ────────────────────────────────────────────────────────────────────

st.title("DI Authority Review")

vols = available_volumes()
if vols:
    vol = st.sidebar.selectbox("Volume", vols, index=len(vols) - 1)
else:
    # No review CSVs yet — let user enter a volume number for the pipeline
    _vn = st.sidebar.number_input("Volume number", min_value=1, value=4, step=1)
    vol = f"vol{int(_vn):02d}"
    st.sidebar.caption("No review CSVs found yet. Run pipeline steps 3-5 to create them.")

st.sidebar.markdown("---")
try:
    rel = config.REVIEW_DIR.relative_to(Path.cwd())
except ValueError:
    rel = config.REVIEW_DIR
st.sidebar.caption(f"Review dir: `{rel}`")

vn = vol_num(vol)
queue_path    = config.REVIEW_DIR / f"{vol}_review_queue.csv"
persons_path  = config.REVIEW_DIR / f"{vol}_persons_new.csv"
places_path   = config.REVIEW_DIR / f"{vol}_places_new.csv"
geocoded_path = config.REVIEW_DIR / f"{vol}_places_new_geocoded.csv"
auth_pl_path  = SCRIPTS / "place_names_authority.csv"
auth_pe_path  = SCRIPTS / "person_names_authority.csv"

tab_pipeline, tab_queue, tab_entities, tab_authority = st.tabs(
    ["Pipeline", "Review Queue", "New Entities", "Authority Browser"]
)


# ── tab 0: pipeline ───────────────────────────────────────────────────────────

with tab_pipeline:
    st.caption(
        "Run each step in order for the selected volume. "
        "Steps that require manual review prompt you to switch to the other tabs."
    )

    any_running = False

    # ── One-time setup ───────────────────────────────────────────────────────
    with st.expander(
        step_label("One-time setup — seed authority CSVs from XLSX", "setup_pl", "setup_pe")
    ):
        st.caption(
            "Run once per machine, or after the master XLSX has changed significantly. "
            "Safe to re-run with `--overwrite`."
        )
        col_pl, col_pe = st.columns(2)

        with col_pl:
            overwrite_pl = st.checkbox("--overwrite", key="setup_pl_ow")
            cmd_pl = [PYTHON, "seed_place_names.py"] + (["--overwrite"] if overwrite_pl else [])
            if st.button("Seed place authority", key="btn_setup_pl", disabled=is_running("setup_pl")):
                run_command(cmd_pl, "setup_pl")
                st.rerun()
            if step_output("setup_pl"):
                any_running = True

        with col_pe:
            overwrite_pe = st.checkbox("--overwrite", key="setup_pe_ow")
            cmd_pe = [PYTHON, "seed_person_names.py"] + (["--overwrite"] if overwrite_pe else [])
            if st.button("Seed person authority", key="btn_setup_pe", disabled=is_running("setup_pe")):
                run_command(cmd_pe, "setup_pe")
                st.rerun()
            if step_output("setup_pe"):
                any_running = True

    # ── Step 1 ───────────────────────────────────────────────────────────────
    s1_key = f"s1_{vol}"
    with st.expander(step_label("Step 1 — Extract charter text from PDF", s1_key)):
        st.caption(
            "Splits the PDF into one .txt file per charter. "
            "Re-running overwrites existing segments."
        )
        pdf_input = st.text_input(
            "PDF path",
            key="s1_pdf",
            placeholder=str(config.PDF_DIR / f"Diplomatarium_Islandicum___Bindi_{vn}.pdf"),
        )
        if st.button(
            "Run",
            key="btn_s1",
            disabled=is_running(s1_key) or not pdf_input.strip(),
        ):
            run_command(
                [PYTHON, "01_extract_text.py", "--pdf", pdf_input.strip(), "--vol", str(vn)],
                s1_key,
            )
            st.rerun()
        if step_output(s1_key):
            any_running = True

    # ── Step 2 ───────────────────────────────────────────────────────────────
    s2_key = f"s2_{vol}"
    with st.expander(step_label("Step 2 — Extract entities with Claude API", s2_key)):
        st.caption(
            "Sends each charter to the Claude API. Large volumes take several minutes. "
            "Results append incrementally so you can pause and resume with batch ranges."
        )
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
            run_command(cmd2, s2_key)
            st.rerun()
        if step_output(s2_key):
            any_running = True

    # ── Step 3 ───────────────────────────────────────────────────────────────
    s3_key = f"s3_{vol}"
    with st.expander(step_label("Step 3 — Resolve entities against authority files", s3_key)):
        st.caption(
            "Fuzzy-matches names against the authority. "
            "Scores >=85 are auto-assigned; 60-84 go to the review queue; <60 become new entities."
        )
        if st.button("Run", key="btn_s3", disabled=is_running(s3_key)):
            run_command([PYTHON, "03_resolve_entities.py", "--vol", str(vn)], s3_key)
            st.rerun()
        if step_output(s3_key):
            any_running = True

    # ── Step 4 ───────────────────────────────────────────────────────────────
    s4_key = f"s4_{vol}"
    with st.expander(step_label("Step 4 — Geocode new places via Wikidata", s4_key)):
        st.caption("Queries Wikidata SPARQL for coordinates of new places.")
        if st.button("Run", key="btn_s4", disabled=is_running(s4_key)):
            run_command([PYTHON, "04_lookup_coords.py", "--vol", str(vn)], s4_key)
            st.rerun()
        if step_output(s4_key):
            any_running = True

    # ── Step 4b ───────────────────────────────────────────────────────────────
    s4b_ann_key = f"s4b_ann_{vol}"
    s4b_app_key = f"s4b_app_{vol}"
    with st.expander(
        step_label("Step 4b — Reconcile place names", s4b_ann_key, s4b_app_key)
    ):
        geocoded_csv = str(geocoded_path)
        col_ann, col_app = st.columns(2)

        with col_ann:
            st.caption("First: annotate the geocoded CSV with proposed authority matches.")
            if st.button(
                "Annotate CSV", key="btn_s4b_ann", disabled=is_running(s4b_ann_key)
            ):
                run_command(
                    [PYTHON, "04b_propagate_corrections.py", "--csv", geocoded_csv, "--annotate"],
                    s4b_ann_key,
                )
                st.rerun()
            if step_output(s4b_ann_key):
                any_running = True

        with col_app:
            st.caption(
                "Then: review in **New Entities > Places**, set `review_status`, and apply."
            )
            if st.button(
                "Apply decisions", key="btn_s4b_app", disabled=is_running(s4b_app_key)
            ):
                run_command(
                    [PYTHON, "04b_propagate_corrections.py", "--csv", geocoded_csv],
                    s4b_app_key,
                )
                st.rerun()
            if step_output(s4b_app_key):
                any_running = True

    # ── Step 4c ───────────────────────────────────────────────────────────────
    s4c_dr_key = f"s4c_dr_{vol}"
    s4c_key = f"s4c_{vol}"
    with st.expander(step_label("Step 4c — Add new places to authority", s4c_key)):
        st.caption(
            "Promotes rows with `review_status=add` from the geocoded CSV "
            "into `place_names_authority.csv`."
        )
        col_dr, col_ap = st.columns(2)
        with col_dr:
            if st.button("Dry run", key="btn_s4c_dr", disabled=is_running(s4c_dr_key)):
                run_command(
                    [PYTHON, "04c_add_to_authority.py", "--csv", str(geocoded_path), "--dry-run"],
                    s4c_dr_key,
                )
                st.rerun()
            if step_output(s4c_dr_key):
                any_running = True
        with col_ap:
            if st.button("Apply", key="btn_s4c", disabled=is_running(s4c_key)):
                run_command(
                    [PYTHON, "04c_add_to_authority.py", "--csv", str(geocoded_path)],
                    s4c_key,
                )
                st.rerun()
            if step_output(s4c_key):
                any_running = True

    # ── Step 5 ───────────────────────────────────────────────────────────────
    s5_key = f"s5_{vol}"
    with st.expander(step_label("Step 5 — Export review CSVs", s5_key)):
        st.caption(
            "Writes charters.csv, persons_new.csv, places_new.csv, and review_queue.csv "
            "to the review directory."
        )
        if st.button("Run", key="btn_s5", disabled=is_running(s5_key)):
            # Clear cached review data so the tabs reload fresh CSVs after this step
            for k in [f"{vol}_queue", f"{vol}_persons", f"{vol}_places"]:
                st.session_state.pop(k, None)
            run_command([PYTHON, "05_export_csvs.py", "--vol", str(vn)], s5_key)
            st.rerun()
        if step_output(s5_key):
            any_running = True
        if st.session_state.get(s5_key, {}).get("status") == "done":
            st.info(
                "Review queue and new entity CSVs are ready. "
                "Switch to the **Review Queue** and **New Entities** tabs to triage, "
                "then return here to continue with steps 5b onward."
            )

    # ── Step 5b ───────────────────────────────────────────────────────────────
    s5b_key = f"s5b_{vol}"
    with st.expander(step_label("Step 5b — Rescan charter review flags", s5b_key)):
        st.caption(
            "Re-checks charters.csv for unresolved REVIEW: prefixes after manual edits. "
            "Run this after resolving flags in the charters CSV before proceeding to step 6."
        )
        if st.button("Run", key="btn_s5b", disabled=is_running(s5b_key)):
            run_command([PYTHON, "05b_rescan_flags.py", "--vol", str(vn)], s5b_key)
            st.rerun()
        if step_output(s5b_key):
            any_running = True

    # ── Step 4d ───────────────────────────────────────────────────────────────
    s4d_dr_key = f"s4d_dr_{vol}"
    s4d_key = f"s4d_{vol}"
    with st.expander(step_label("Step 4d — Add new persons to authority", s4d_key)):
        st.caption(
            "Promotes rows with `review_status=add` from persons_new.csv "
            "into `person_names_authority.csv`."
        )
        col_dr, col_ap = st.columns(2)
        with col_dr:
            if st.button("Dry run", key="btn_s4d_dr", disabled=is_running(s4d_dr_key)):
                run_command(
                    [PYTHON, "04d_add_to_person_authority.py", "--csv", str(persons_path), "--dry-run"],
                    s4d_dr_key,
                )
                st.rerun()
            if step_output(s4d_dr_key):
                any_running = True
        with col_ap:
            if st.button("Apply", key="btn_s4d", disabled=is_running(s4d_key)):
                run_command(
                    [PYTHON, "04d_add_to_person_authority.py", "--csv", str(persons_path)],
                    s4d_key,
                )
                st.rerun()
            if step_output(s4d_key):
                any_running = True

    # ── Step 6 ───────────────────────────────────────────────────────────────
    s6_dr_key = f"s6_dr_{vol}"
    s6_key = f"s6_{vol}"
    with st.expander(step_label("Step 6 — Merge into authority XLSX", s6_key)):
        st.caption(
            "Merges approved charters, persons, and places into a copy of the authority XLSX. "
            "The original is never modified."
        )
        col_dr, col_ap = st.columns(2)
        with col_dr:
            if st.button("Dry run", key="btn_s6_dr", disabled=is_running(s6_dr_key)):
                run_command(
                    [PYTHON, "06_merge_into_xlsx.py", "--vol", str(vn), "--dry-run"],
                    s6_dr_key,
                )
                st.rerun()
            if step_output(s6_dr_key):
                any_running = True
        with col_ap:
            if st.button("Apply", key="btn_s6", disabled=is_running(s6_key)):
                run_command(
                    [PYTHON, "06_merge_into_xlsx.py", "--vol", str(vn)],
                    s6_key,
                )
                st.rerun()
            if step_output(s6_key):
                any_running = True

    # ── Keep polling while any step is running ────────────────────────────────
    if any_running:
        time.sleep(0.5)
        st.rerun()


# ── tab 1: review queue ───────────────────────────────────────────────────────

with tab_queue:
    qkey = f"{vol}_queue"
    if qkey not in st.session_state:
        st.session_state[qkey] = load_csv(queue_path, add_cols={"decision": ""})

    df_q = st.session_state[qkey]

    if df_q.empty:
        st.info("No review queue for this volume. Run pipeline steps 3 and 5 first.")
    else:
        n_done = (df_q["decision"].str.strip() != "").sum()
        st.caption(
            f"**{n_done} / {len(df_q)}** decisions recorded — "
            "click column headers to sort · edit the **Decision** column"
        )

        edited_q = st.data_editor(
            df_q,
            key=f"ed_{qkey}",
            use_container_width=True,
            num_rows="fixed",
            hide_index=True,
            column_config={
                "type": st.column_config.TextColumn("Type", width="small", disabled=True),
                "extracted_name": st.column_config.TextColumn(
                    "Extracted name", width="medium", disabled=True
                ),
                "closest_match": st.column_config.TextColumn(
                    "Closest match", width="medium", disabled=True
                ),
                "match_id": st.column_config.TextColumn(
                    "Proposed ID", width="small", disabled=True
                ),
                "score": st.column_config.NumberColumn(
                    "Score", format="%.1f", width="small", disabled=True
                ),
                "role_category": st.column_config.TextColumn(
                    "Role", width="small", disabled=True
                ),
                "role": st.column_config.TextColumn(
                    "Place role", width="small", disabled=True
                ),
                "charter_filename": st.column_config.TextColumn(
                    "Charter", width="small", disabled=True
                ),
                "charter_date": st.column_config.TextColumn(
                    "Date", width="small", disabled=True
                ),
                "decision": st.column_config.SelectboxColumn(
                    "Decision",
                    options=["", "accept", "reject"],
                    width="small",
                    help="accept = use proposed ID  ·  reject = treat as new entity",
                ),
            },
        )

        autosave(qkey, edited_q, queue_path)
        st.session_state[qkey] = edited_q


# ── tab 2: new entities ───────────────────────────────────────────────────────

with tab_entities:
    sub_p, sub_pl = st.tabs(["Persons", "Places"])

    # ── persons ──────────────────────────────────────────────────────────────
    with sub_p:
        pkey = f"{vol}_persons"
        if pkey not in st.session_state:
            st.session_state[pkey] = load_csv(
                persons_path, add_cols={"review_status": "", "wikidata_id": ""}
            )

        df_p = st.session_state[pkey]

        if df_p.empty:
            st.info("No new persons for this volume. Run pipeline steps 3 and 5 first.")
        else:
            n_done = (df_p["review_status"].str.strip() != "").sum()
            st.caption(
                f"**{n_done} / {len(df_p)}** decisions recorded — "
                "set **Status** for each row (ok / skip / add)"
            )

            edited_p = st.data_editor(
                with_wikidata_links(df_p),
                key=f"ed_{pkey}",
                use_container_width=True,
                num_rows="fixed",
                hide_index=True,
                column_order=[
                    "person_id", "review_status", "canonical_name",
                    "wikidata_id", "wikidata_link",
                    "variant_names", "patronymic", "occupation", "title",
                    "floruit_start", "floruit_end", "gender",
                    "associated_places", "notes", "sources",
                ],
                column_config={
                    "person_id": st.column_config.TextColumn(
                        "ID", width="small", disabled=True
                    ),
                    "review_status": st.column_config.SelectboxColumn(
                        "Status",
                        options=["", "ok", "skip", "add"],
                        width="small",
                        help="ok = include in charter data  ·  add = also promote to authority file",
                    ),
                    "canonical_name": st.column_config.TextColumn(
                        "Canonical name", width="medium"
                    ),
                    "wikidata_id": st.column_config.TextColumn(
                        "Wikidata ID", width="small"
                    ),
                    "wikidata_link": st.column_config.LinkColumn(
                        "Wikidata", width="small", disabled=True
                    ),
                    "variant_names": st.column_config.TextColumn(
                        "Variants", width="large"
                    ),
                    "patronymic": st.column_config.TextColumn(
                        "Patronymic", width="small"
                    ),
                    "occupation": st.column_config.TextColumn(
                        "Occupation", width="medium"
                    ),
                    "title": st.column_config.TextColumn("Title", width="small"),
                    "floruit_start": st.column_config.TextColumn(
                        "Fl. start", width="small"
                    ),
                    "floruit_end": st.column_config.TextColumn(
                        "Fl. end", width="small"
                    ),
                    "gender": st.column_config.SelectboxColumn(
                        "Gender",
                        options=["", "M", "F", "unknown"],
                        width="small",
                    ),
                    "associated_places": st.column_config.TextColumn(
                        "Places", width="medium"
                    ),
                    "notes": st.column_config.TextColumn("Notes", width="large"),
                    "sources": st.column_config.TextColumn(
                        "Sources", width="small", disabled=True
                    ),
                },
            )

            save_p = edited_p.drop(columns=["wikidata_link"])
            autosave(pkey, save_p, persons_path)
            st.session_state[pkey] = save_p

    # ── places ───────────────────────────────────────────────────────────────
    with sub_pl:
        plkey = f"{vol}_places"
        if plkey not in st.session_state:
            st.session_state[plkey] = load_csv(
                places_path, add_cols={"review_status": "", "wikidata_id": ""}
            )

        df_pl = st.session_state[plkey]

        if df_pl.empty:
            st.info("No new places for this volume. Run pipeline steps 3 and 5 first.")
        else:
            n_done = (df_pl["review_status"].str.strip() != "").sum()
            st.caption(
                f"**{n_done} / {len(df_pl)}** decisions recorded — "
                "set **Status** for each row (ok / skip / add)"
            )

            edited_pl = st.data_editor(
                with_wikidata_links(df_pl),
                key=f"ed_{plkey}",
                use_container_width=True,
                num_rows="fixed",
                hide_index=True,
                column_order=[
                    "place_id", "review_status", "canonical_name",
                    "wikidata_id", "wikidata_link",
                    "variant_names", "place_type",
                    "coordinates_lat", "coordinates_long",
                    "region", "district", "modern_equivalent", "notes", "sources",
                ],
                column_config={
                    "place_id": st.column_config.TextColumn(
                        "ID", width="small", disabled=True
                    ),
                    "review_status": st.column_config.SelectboxColumn(
                        "Status",
                        options=["", "ok", "skip", "add"],
                        width="small",
                        help="ok = include in charter data  ·  add = also promote to authority file",
                    ),
                    "canonical_name": st.column_config.TextColumn(
                        "Canonical name", width="medium"
                    ),
                    "wikidata_id": st.column_config.TextColumn(
                        "Wikidata ID", width="small"
                    ),
                    "wikidata_link": st.column_config.LinkColumn(
                        "Wikidata", width="small", disabled=True
                    ),
                    "variant_names": st.column_config.TextColumn(
                        "Variants", width="large"
                    ),
                    "place_type": st.column_config.TextColumn(
                        "Type", width="small"
                    ),
                    "coordinates_lat": st.column_config.TextColumn(
                        "Lat", width="small"
                    ),
                    "coordinates_long": st.column_config.TextColumn(
                        "Lon", width="small"
                    ),
                    "region": st.column_config.TextColumn("Region", width="small"),
                    "district": st.column_config.TextColumn(
                        "District", width="small"
                    ),
                    "modern_equivalent": st.column_config.TextColumn(
                        "Modern equiv.", width="medium"
                    ),
                    "notes": st.column_config.TextColumn("Notes", width="large"),
                    "sources": st.column_config.TextColumn(
                        "Sources", width="small", disabled=True
                    ),
                },
            )

            save_pl = edited_pl.drop(columns=["wikidata_link"])
            autosave(plkey, save_pl, places_path)
            st.session_state[plkey] = save_pl


# ── tab 3: authority browser ──────────────────────────────────────────────────

with tab_authority:
    search = st.text_input(
        "Search",
        placeholder="Filter by any field...",
        key="auth_search",
    )
    auth_pl_tab, auth_pe_tab = st.tabs(["Places", "Persons"])

    with auth_pl_tab:
        auth_pl = load_csv(auth_pl_path)
        if auth_pl.empty:
            st.info("place_names_authority.csv not found.")
        else:
            if search:
                mask = auth_pl.apply(
                    lambda r: search.lower() in " ".join(r.values.astype(str)).lower(),
                    axis=1,
                )
                auth_pl = auth_pl[mask]
            st.caption(f"{len(auth_pl)} entries")
            st.dataframe(auth_pl, use_container_width=True, hide_index=True)

    with auth_pe_tab:
        auth_pe = load_csv(auth_pe_path)
        if auth_pe.empty:
            st.info(
                "person_names_authority.csv not found -- run `seed_person_names.py` first."
            )
        else:
            if search:
                mask = auth_pe.apply(
                    lambda r: search.lower() in " ".join(r.values.astype(str)).lower(),
                    axis=1,
                )
                auth_pe = auth_pe[mask]
            st.caption(f"{len(auth_pe)} entries")
            st.dataframe(auth_pe, use_container_width=True, hide_index=True)
