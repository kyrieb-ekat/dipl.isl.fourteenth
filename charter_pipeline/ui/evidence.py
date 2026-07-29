"""Evidence panes: the charters behind whatever the card is comparing.

The card on its own shows extracted fields, which often cannot settle a
decision -- "Einar, priest, 1340" vs "Einar, layman, 1341" at name score 100
is unresolvable from those alone. These panes show what the records are
actually attested by.

Each pane is collapsed by default and only queries when opened. That is
deliberate: review_queue's whole design is a cheap index plus an expensive
materialize() for one item, and fetching evidence eagerly for every card would
undo it. An st.expander body is not executed while collapsed, so a closed pane
costs nothing.
"""
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
import db  # noqa: E402

# ── source text ──────────────────────────────────────────────────────────────


@st.cache_data(show_spinner=False)
def load_charter_text(volume: int, sequence: int) -> str | None:
    """The transcribed segment for one charter, or None if absent.

    Cheap enough to show inline: segments are median ~1.8 KB and only 21 of
    791 exceed 20 KB. Cached because the same charter is often revisited while
    working through a cluster.
    """
    path = config.SEGMENTS_DIR / f"vol{volume:02d}" / f"DI_{volume:02d}_{sequence:04d}.txt"
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _highlight(text: str, needle: str) -> str:
    """Marks occurrences of `needle`, case-insensitively.

    Plain string scanning rather than a regex: extracted names contain
    characters a naive pattern would treat as syntax, and the Icelandic forms
    here are exactly the kind of input that breaks that quietly.
    """
    if not needle or not needle.strip():
        return text
    needle = needle.strip()
    low_text, low_needle = text.lower(), needle.lower()
    out, i = [], 0
    while True:
        j = low_text.find(low_needle, i)
        if j < 0:
            out.append(text[i:])
            break
        out.append(text[i:j])
        out.append(f'<mark class="diff-add">{text[j:j + len(needle)]}</mark>')
        i = j + len(needle)
    return "".join(out)


# ── panes ────────────────────────────────────────────────────────────────────

_APPEARANCE_COLUMNS = {
    "person": [("di_year", "Year"), ("di_reference", "DI ref."),
               ("doc_type", "Type"), ("role_category", "Role"),
               ("extracted_name", "As spelled"), ("qualifier", "Qualifier")],
    "place": [("di_year", "Year"), ("di_reference", "DI ref."),
              ("doc_type", "Type"), ("role", "Role"),
              ("extracted_name", "As spelled"), ("region", "Region")],
}


def _appearances_frame(entity_type: str, pk: int) -> pd.DataFrame:
    return (db.get_person_appearances(pk) if entity_type == "person"
            else db.get_place_appearances(pk))


def render_appearances(entity_type: str, pk: int, label: str, key: str) -> None:
    """One side's charter attestations."""
    df = _appearances_frame(entity_type, pk)
    if df.empty:
        st.caption(f"**{label}** — no charter appearances recorded.")
        # Worth saying why rather than leaving a blank: 21 authority-imported
        # persons are in this state, which also means nothing in the corpus
        # supports them.
        st.caption("Nothing in the corpus attests this record, so there is no "
                   "charter evidence to compare.")
        return

    years = df["di_year"].dropna()
    span = f"{int(years.min())}–{int(years.max())}" if len(years) else "undated"
    st.caption(f"**{label}** — {df['charter_pk'].nunique()} charter(s), {span}")

    cols = [(c, t) for c, t in _APPEARANCE_COLUMNS[entity_type] if c in df.columns]
    view = df[[c for c, _ in cols]].rename(columns=dict(cols))
    if "Year" in view.columns:
        # Render as a plain string: a nullable int column shows up as 1340.0.
        view["Year"] = view["Year"].apply(lambda v: "" if pd.isna(v) else str(int(v)))
    st.dataframe(view, hide_index=True, use_container_width=True,
                 height=min(320, 40 + 35 * len(view)), key=key)


def render_charter_text(volume: int, sequence: int, highlight: str = "",
                         di_reference: str = "") -> None:
    text = load_charter_text(int(volume), int(sequence))
    ref = di_reference or f"vol{int(volume):02d} seq {int(sequence)}"
    if text is None:
        st.caption(f"No transcribed segment on disk for {ref}.")
        return
    if not text.strip():
        # Some segments are header-only stubs; say so rather than showing blank.
        st.caption(f"The segment for {ref} is empty (header-only stub).")
        return
    st.caption(f"**{ref}** — {len(text):,} characters")
    st.markdown(
        f'<div style="max-height:340px;overflow-y:auto;white-space:pre-wrap;'
        f'font-size:0.85em;line-height:1.45;padding:0.6em;'
        f'border:1px solid rgba(128,128,128,0.35);border-radius:4px;">'
        f'{_highlight(text, highlight)}</div>',
        unsafe_allow_html=True)


@st.cache_data(show_spinner=False)
def load_page_image_bytes(volume: int, page: int) -> bytes | None:
    """Mirrors load_charter_text's disk-read-wrapped-in-st.cache_data pattern.
    Extraction itself is cheap (~0.05-0.2s via pdfimages -j, see pdf_pages.py)
    and only a small, bounded subset of pages will ever actually be opened,
    so this generates strictly on first request rather than pre-rendering --
    same reasoning as this module's own docstring gives for collapsed
    expanders not querying eagerly."""
    import pdf_pages
    pdf_path = pdf_pages.resolve_pdf_path(volume)
    if pdf_path is None:
        return None
    cache_dir = config.OUTPUT_DIR / "page_images" / f"vol{volume:02d}"
    img_path = pdf_pages.extract_page_image(pdf_path, page, cache_dir)
    if img_path is None:
        return None
    return img_path.read_bytes()


def render_page_image(volume: int, page: int, caption: str = "") -> None:
    data = load_page_image_bytes(int(volume), int(page))
    if data is None:
        st.caption(f"No page image available for vol{int(volume):02d}, page {int(page)}.")
        return
    st.image(data, caption=caption or f"vol{int(volume):02d}, page {int(page)}",
             use_container_width=True)


def render_charter_cast(charter_pk: int, exclude_person_pk=None,
                         exclude_place_pk=None) -> None:
    cast = db.get_charter_cast(charter_pk, exclude_person_pk=exclude_person_pk,
                               exclude_place_pk=exclude_place_pk)
    left, right = st.columns(2)
    with left:
        st.caption("**Others named in this charter**")
        people = cast["persons"]
        if people.empty:
            st.caption("_none_")
        else:
            show = people[["extracted_name", "role_category", "qualifier"]].rename(
                columns={"extracted_name": "Name", "role_category": "Role",
                         "qualifier": "Qualifier"})
            st.dataframe(show, hide_index=True, use_container_width=True,
                         height=min(240, 40 + 35 * len(show)))
    with right:
        st.caption("**Places named in this charter**")
        places = cast["places"]
        if places.empty:
            st.caption("_none_")
        else:
            show = places[["extracted_name", "role"]].rename(
                columns={"extracted_name": "Place", "role": "Role"})
            st.dataframe(show, hide_index=True, use_container_width=True,
                         height=min(240, 40 + 35 * len(show)))


# ── what a given card is comparing ───────────────────────────────────────────

def evidence_targets(rq_item) -> list[dict]:
    """The entities whose evidence is worth showing for this card.

    Derived from the item's payload rather than re-queried. Sides that have no
    charter evidence by construction (an external nafnid.is record) are simply
    absent rather than rendered empty.
    """
    p, t = rq_item.payload or {}, rq_item.item_type

    def side(label, entity_type, pk):
        # .get() throughout, and skip anything absent: evidence is
        # supplementary, so a payload that doesn't carry an id must degrade to
        # "no evidence shown" rather than break the decision buttons.
        # `is not None`, not truthiness -- pk 0 is a legal rowid.
        return ([{"label": label, "entity_type": entity_type, "pk": pk}]
                if pk is not None else [])

    if t in ("new_person", "new_place"):
        kind = "person" if t == "new_person" else "place"
        return (side("This record", kind, p.get("pk"))
                + side("Authority match", kind, p.get("match_pk")))
    if t == "person_dup":
        return (side("Side A", "person", p.get("person_a_pk"))
                + side("Side B", "person", p.get("person_b_pk")))
    if t == "place_dup":
        # The nafnid.is candidate is external; only the DI place has charters.
        return side("DI place", "place", p.get("place_pk"))
    if t == "review_item":
        return side("Proposed match", p.get("entity_type") or "person",
                    p.get("match_pk"))
    return []


def _charters_for(targets: list[dict], limit: int = 12) -> list[dict]:
    """Charters to offer source text for, newest evidence first."""
    seen, out = set(), []
    for tgt in targets:
        df = _appearances_frame(tgt["entity_type"], tgt["pk"])
        for _, row in df.iterrows():
            key = int(row["charter_pk"])
            if key in seen:
                continue
            seen.add(key)
            volume, sequence = int(row["volume"]), int(row["sequence"])
            ref = row["di_reference"] or ""
            year = "" if pd.isna(row["di_year"]) else f" ({int(row['di_year'])})"
            out.append({
                "charter_pk": key,
                "volume": volume,
                "sequence": sequence,
                "di_reference": ref,
                "extracted_name": row["extracted_name"] or "",
                "label": f"{ref or f'vol{volume:02d} seq {sequence}'}{year}",
            })
            if len(out) >= limit:
                return out
    return out


def _own_charter(rq_item) -> list[dict]:
    """For a review_item the mention itself lives in one specific charter, which
    is the most relevant text of all -- it isn't reachable via appearances
    because the mention is still unresolved (person_pk/place_pk is NULL)."""
    charter_pk = rq_item.payload.get("charter_pk")
    if not charter_pk:
        return []
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT charter_pk, volume, sequence, di_reference, di_year "
            "FROM charters WHERE charter_pk = ?", (charter_pk,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return []
    ref = row["di_reference"] or f"vol{row['volume']:02d} seq {row['sequence']}"
    year = f" ({row['di_year']})" if row["di_year"] else ""
    return [{
        "charter_pk": row["charter_pk"], "volume": row["volume"],
        "sequence": row["sequence"], "di_reference": row["di_reference"] or "",
        "extracted_name": rq_item.header.split("—")[0].strip(),
        "label": f"{ref}{year}  ← this mention",
    }]


def render_panes(rq_item, key_prefix: str) -> None:
    """The three evidence panes for one card, all collapsed by default.

    Collapsed matters: an st.expander body doesn't execute while closed, so
    none of these query until opened. Fetching eagerly would undo
    review_queue's cheap-index/expensive-materialize split.
    """
    targets = evidence_targets(rq_item)
    # The review_item's own charter first -- it's the mention being judged.
    charters = _own_charter(rq_item) + _charters_for(targets)

    with st.expander("📜 Charter appearances", expanded=False):
        if not targets:
            st.caption("Nothing to show for this item type.")
        else:
            for i, tgt in enumerate(targets):
                render_appearances(tgt["entity_type"], tgt["pk"], tgt["label"],
                                   key=f"{key_prefix}_app_{i}")

    with st.expander("📖 Charter source text", expanded=False):
        if not charters:
            st.caption("No charters to show text for.")
        else:
            chosen = _pick_charter(charters, f"{key_prefix}_txt")
            render_charter_text(chosen["volume"], chosen["sequence"],
                                highlight=chosen["extracted_name"],
                                di_reference=chosen["di_reference"])

    with st.expander("👥 Who else is in the charter", expanded=False):
        if not charters:
            st.caption("No charters to show.")
        else:
            chosen = _pick_charter(charters, f"{key_prefix}_cast")
            person_pks = [t["pk"] for t in targets if t["entity_type"] == "person"]
            place_pks = [t["pk"] for t in targets if t["entity_type"] == "place"]
            render_charter_cast(chosen["charter_pk"],
                                exclude_person_pk=person_pks[0] if person_pks else None,
                                exclude_place_pk=place_pks[0] if place_pks else None)


def _pick_charter(charters: list[dict], key: str) -> dict:
    if len(charters) == 1:
        return charters[0]
    labels = [c["label"] for c in charters]
    picked = st.selectbox("Charter", labels, key=f"{key}_pick")
    return charters[labels.index(picked)]
