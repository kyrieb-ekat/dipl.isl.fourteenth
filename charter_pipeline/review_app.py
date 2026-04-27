"""
DI Charter Authority Review
Run: streamlit run charter_pipeline/review_app.py
"""
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent))
import config

st.set_page_config(page_title="DI Authority Review", layout="wide")


# ── helpers ────────────────────────────────────────────────────────────────────


def load_csv(path: Path, add_cols: dict | None = None) -> pd.DataFrame:
    """Read CSV as strings; insert any missing columns with their default values."""
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path, dtype=str).fillna("")
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


# ── sidebar ────────────────────────────────────────────────────────────────────

st.title("DI Authority Review")

vols = available_volumes()
if not vols:
    st.error("No review CSVs found. Run pipeline steps 3-5 first.")
    st.stop()

vol = st.sidebar.selectbox("Volume", vols, index=len(vols) - 1)
st.sidebar.markdown("---")
try:
    rel = config.REVIEW_DIR.relative_to(Path.cwd())
except ValueError:
    rel = config.REVIEW_DIR
st.sidebar.caption(f"Review dir: `{rel}`")

queue_path   = config.REVIEW_DIR / f"{vol}_review_queue.csv"
persons_path = config.REVIEW_DIR / f"{vol}_persons_new.csv"
places_path  = config.REVIEW_DIR / f"{vol}_places_new.csv"
auth_pl_path = Path(__file__).parent / "place_names_authority.csv"
auth_pe_path = Path(__file__).parent / "person_names_authority.csv"

tab_queue, tab_entities, tab_authority = st.tabs(
    ["Review Queue", "New Entities", "Authority Browser"]
)


# ── tab 1: review queue ───────────────────────────────────────────────────────

with tab_queue:
    qkey = f"{vol}_queue"
    if qkey not in st.session_state:
        st.session_state[qkey] = load_csv(queue_path, add_cols={"decision": ""})

    df_q = st.session_state[qkey]

    if df_q.empty:
        st.info("No review queue for this volume.")
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
            st.info("No new persons for this volume.")
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
            st.info("No new places for this volume.")
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
