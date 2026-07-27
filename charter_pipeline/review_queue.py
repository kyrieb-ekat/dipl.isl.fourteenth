"""
Unified review queue: merges four structurally different pending-decision
sources (new persons/places awaiting curation, person duplicate candidates,
place duplicate candidates, open review-queue items) into one list of
QueueItem cards, so a reviewer can work through every kind of decision in one
keyboard-driven pass instead of switching between grid-based tabs.

Every mutation here calls straight into an existing db.py function -- no new
mutation codepaths, only new read/assembly logic on top of what the
grid-based tabs already use.

Two-phase build, not one: build_queue_index(filt) does the cheap SQL fetch
+ filter/sort for EVERY matching row, but stops short of the expensive part
(materialize() below) -- for new_person/new_place specifically, that's a
db.search_authority() fuzzy-match call per row, which at real data scale
(thousands of pending rows) made every single render (i.e. every button
click, since Streamlit reruns the whole script) do thousands of fuzzy
matches for items nobody was even looking at. materialize(entry) does that
expensive work for exactly the one entry about to be displayed.
"""
from dataclasses import dataclass, field

import pandas as pd

import db
from diff_render import blank

# Name-similarity score (from db.search_authority's rapidfuzz token_sort_ratio)
# at or above which a New Entities match is flagged as high-confidence --
# merge is always offered regardless of score (a lower score can still be a
# correct match the reviewer can see for themselves in the diff; forcing
# "Add to authority" instead when merge wasn't offered was creating avoidable
# duplicate canonical entries), but this threshold controls how it's
# presented: primary-styled and ordered first when at/above it, secondary and
# ordered last (just before "next") otherwise.
MERGE_SUGGEST_THRESHOLD = 90

ALL_ITEM_TYPES = {"new_person", "new_place", "person_dup", "place_dup", "review_item"}

# Item types where selecting 2+ entries and merging them together (peer-to-
# peer, not "into an authority match") is semantically real -- place_dup
# compares a DI place against an external, non-mergeable nafnid.is record,
# and review_item is accept/reject on a charter reference, not a mergeable
# entity, so both are excluded.
MERGEABLE_ITEM_TYPES = {"new_person", "new_place", "person_dup"}


@dataclass
class QueueAction:
    hotkey: str          # single-char keyboard binding, e.g. "a"; unique within one item's action list
    action: str           # semantic name apply_action() dispatches on, e.g. "add" -- NOT the hotkey
    label: str            # button/legend text, e.g. "Add to authority (a)"
    style: str = "secondary"   # "primary" | "secondary" | "danger"


@dataclass
class QueueItem:
    item_id: str                  # globally unique, e.g. "new_person:123"
    item_type: str                 # one of ALL_ITEM_TYPES
    volume: int | None
    header: str
    subheader: str
    left_label: str
    right_label: str
    diff_rows: list                # list[(label, left_val_str, right_val_str)]
    actions: list                  # list[QueueAction]
    payload: dict                  # ids the action handlers need
    sort_score: float = 0.0        # higher = reviewed first within default sort


@dataclass
class QueueIndexEntry:
    """The cheap half of a QueueItem -- enough to list, filter, sort, and
    count the queue without paying for the expensive part. materialize()
    turns exactly one of these into a full QueueItem."""
    item_id: str
    item_type: str
    volume: int | None
    sort_score: float     # cheap-only: real score for person_dup/place_dup/review_item
                            # (already a column on their row); 0.0 for new_person/new_place,
                            # since a real value would require the deferred fuzzy match --
                            # those two types fall back to their DB query's own
                            # highest-confidence-first ORDER BY instead.
    sort_name: str          # lowercased, for "name" sort -- cheap for every type
    list_label: str         # human-readable one-line label for the List+detail view's
                              # list pane -- cheap for every type, built from the same
                              # already-fetched row dict as sort_name/search_text, no
                              # materialize() needed just to list an entry.
    search_text: str        # lowercased raw fields this entry can be found by.
                              # Full fidelity for person_dup/place_dup/review_item (their
                              # subheader/diff text is already all raw row data, nothing
                              # deferred). For new_person/new_place this is name/id only --
                              # a real, minor, documented trade-off: search can no longer
                              # match on the authority-match text (e.g. a score number)
                              # since that isn't computed until materialize().
    row: dict               # the already-fetched DB row; materialize() finishes the job


@dataclass
class QueueFilter:
    volumes: list | None = None                 # None = all volumes
    item_types: set = field(default_factory=lambda: set(ALL_ITEM_TYPES))
    search: str = ""
    confidence: str | None = None                # currently unused -- no caller sets this;
                                                    # kept on the dataclass in case a future caller
                                                    # wants it, but build_queue_index doesn't filter
                                                    # on it (doing so for new_person/new_place would
                                                    # require materializing every row, defeating the
                                                    # point of this module).
    sort: str = "default"                         # "default" | "score_desc" | "score_asc" | "name"
    flagged_only: bool = False                    # new_person only -- surfaces persons.data_quality_flag
                                                     # rows regardless of volume/status/review_status (a
                                                     # flagged row can already be canonical/reviewed), for
                                                     # the 09_flag_transmission_actors.py follow-up workflow.


def _person_diff_rows(left: dict, right: dict | None) -> list:
    from diff_render import COMPARE_ROWS, field_value
    right = right or {}
    return [(label, field_value(left, lk), field_value(right, rk))
            for label, lk, rk in COMPARE_ROWS["person"]]


def _place_diff_rows(left: dict, right: dict | None) -> list:
    from diff_render import COMPARE_ROWS, field_value
    right = right or {}
    return [(label, field_value(left, lk), field_value(right, rk))
            for label, lk, rk in COMPARE_ROWS["place"]]


def _to_int(v):
    """source_volume comes back from pandas as a genuine int only if every
    row in the whole result set happened to be non-null -- a single NULL
    anywhere in that column upcasts the WHOLE column to float64 (numpy int
    arrays can't hold NaN), so e.g. row 4 -> 4.0 even though its own value
    was never missing. Separately, a row's OWN value can be genuinely
    missing too (e.g. a place with no source_volume) -- pandas represents
    that as NaN (a float), NOT None, so `v is None` alone doesn't catch it;
    checking pd.isna() first is required (same pitfall diff_render.blank()
    already guards against). QueueItem.volume must end up a real int or
    None: review_app.py formats it with :02d, which raises on a float and
    int(nan) itself raises ValueError, so both cases must return None here."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    return int(v)


def _is_high_confidence(match: dict | None) -> bool:
    return bool(match) and match.get("_match_score", 0) >= MERGE_SUGGEST_THRESHOLD


def _match_subheader(match: dict | None) -> str:
    if not match:
        return "No plausible authority match found."
    score = match.get("_match_score", 0)
    confidence = "High-confidence" if _is_high_confidence(match) else "Possible"
    return f"{confidence} authority match: {match['display_id']}  (score {score:.0f})"


def _merge_action(match: dict | None):
    """Merge is always offered when any match exists, regardless of score --
    only its styling/position signals confidence (primary + first when
    high-confidence, secondary + last-before-next otherwise), so a reviewer
    can still merge a lower-scoring but visibly-correct match instead of
    being forced into "Add to authority", which would mint a duplicate
    canonical entry alongside the real one."""
    if not match:
        return None
    high = _is_high_confidence(match)
    label = f"Merge into {match['display_id']} (m)" if high else f"Merge into {match['display_id']}? (m)"
    return QueueAction("m", "merge", label, "primary" if high else "secondary")


# ── new_person ────────────────────────────────────────────────────────────

def _index_new_person_items(volumes: list | None, flagged_only: bool = False) -> list:
    entries = []
    df = db.get_persons(flagged_only=True) if flagged_only \
        else db.get_persons(status="provisional", review_status="")
    if volumes:
        df = df[df["source_volume"].isin(volumes)]
    for row in df.to_dict("records"):
        # Bulk to_dict("records") instead of .iterrows() + per-row .to_dict()
        # -- ~3x faster at real data scale (measured: 0.66s -> 0.23s for
        # person_dup's 15,862 rows), and this runs on every render since
        # build_queue_index() is called fresh every time.
        entries.append(QueueIndexEntry(
            item_id=f"new_person:{row['person_pk']}",
            item_type="new_person",
            volume=_to_int(row["source_volume"]),
            sort_score=0.0,
            sort_name=(row["canonical_name"] or "").lower(),
            list_label=f"{row['canonical_name']}  ({row['display_id']})",
            search_text=f"{row['canonical_name']} {row['display_id']}".lower(),
            row=row,
        ))
    return entries


def _materialize_new_person(entry: QueueIndexEntry) -> QueueItem:
    row = entry.row
    matches = db.search_authority("person", row["canonical_name"], limit=1)
    match = matches[0] if matches else None
    merge_action = _merge_action(match)
    high_confidence = _is_high_confidence(match)

    actions = []
    if merge_action and high_confidence:
        actions.append(merge_action)
    actions += [
        QueueAction("o", "ok", "OK (o)"),
        QueueAction("a", "add", "Add to authority (a)", "primary"),
        QueueAction("k", "skip", "Skip (k)"),
    ]
    if merge_action and not high_confidence:
        actions.append(merge_action)
    actions.append(QueueAction("n", "next", "Next / not sure yet (n)"))
    return QueueItem(
        item_id=entry.item_id,
        item_type="new_person",
        volume=entry.volume,
        header=f"{row['canonical_name']}  ({row['display_id']})",
        subheader=_match_subheader(match),
        left_label="New person", right_label="Authority match",
        diff_rows=_person_diff_rows(row, match),
        actions=actions,
        payload={"pk": row["person_pk"], "match_pk": match["person_pk"] if match else None},
        sort_score=match.get("_match_score", 0) if match else 0,
    )


# ── new_place ─────────────────────────────────────────────────────────────

def _index_new_place_items(volumes: list | None) -> list:
    entries = []
    df = db.get_places(status="provisional", review_status="")
    if volumes:
        df = df[df["source_volume"].isin(volumes)]
    for row in df.to_dict("records"):
        # Bulk to_dict("records") instead of .iterrows() + per-row .to_dict()
        # -- ~3x faster at real data scale (measured: 0.66s -> 0.23s for
        # person_dup's 15,862 rows), and this runs on every render since
        # build_queue_index() is called fresh every time.
        entries.append(QueueIndexEntry(
            item_id=f"new_place:{row['place_pk']}",
            item_type="new_place",
            volume=_to_int(row["source_volume"]),
            sort_score=0.0,
            sort_name=(row["canonical_name"] or "").lower(),
            list_label=f"{row['canonical_name']}  ({row['display_id']})",
            search_text=f"{row['canonical_name']} {row['display_id']}".lower(),
            row=row,
        ))
    return entries


def _materialize_new_place(entry: QueueIndexEntry) -> QueueItem:
    row = entry.row
    matches = db.search_authority("place", row["canonical_name"], limit=1)
    match = matches[0] if matches else None
    merge_action = _merge_action(match)
    high_confidence = _is_high_confidence(match)

    actions = []
    if merge_action and high_confidence:
        actions.append(merge_action)
    actions += [
        QueueAction("o", "ok", "OK (o)"),
        QueueAction("a", "add", "Add to authority (a)", "primary"),
        QueueAction("k", "skip", "Skip (k)"),
        QueueAction("x", "no_match", "No match / not a real place (x)"),
    ]
    if merge_action and not high_confidence:
        actions.append(merge_action)
    actions.append(QueueAction("n", "next", "Next / not sure yet (n)"))
    return QueueItem(
        item_id=entry.item_id,
        item_type="new_place",
        volume=entry.volume,
        header=f"{row['canonical_name']}  ({row['display_id']})",
        subheader=_match_subheader(match),
        left_label="New place", right_label="Authority match",
        diff_rows=_place_diff_rows(row, match),
        actions=actions,
        payload={"pk": row["place_pk"], "match_pk": match["place_pk"] if match else None},
        sort_score=match.get("_match_score", 0) if match else 0,
    )


# ── person_dup ────────────────────────────────────────────────────────────

def _index_person_dup_items(volumes: list | None) -> list:
    entries = []
    df = db.get_person_duplicate_candidates(decision="")
    if volumes:
        df = df[df["a_source"].isin([f"vol{v:02d}" for v in volumes]) |
                 df["b_source"].isin([f"vol{v:02d}" for v in volumes])]
    for row in df.to_dict("records"):
        # Bulk to_dict("records") instead of .iterrows() + per-row .to_dict()
        # -- ~3x faster at real data scale (measured: 0.66s -> 0.23s for
        # person_dup's 15,862 rows), and this runs on every render since
        # build_queue_index() is called fresh every time.
        search_text = " ".join(str(row.get(k) or "") for k in (
            "a_canonical_name", "a_display_id", "b_canonical_name", "b_display_id",
            "a_source", "b_source", "a_occupation", "b_occupation", "a_title", "b_title",
            "classification", "confidence",
        )).lower()
        entries.append(QueueIndexEntry(
            item_id=f"person_dup:{row['candidate_pk']}",
            item_type="person_dup",
            volume=None,
            sort_score=row["name_score"] or 0,
            sort_name=f"{row['a_canonical_name']}  {row['b_canonical_name']}".lower(),
            list_label=f"{row['a_canonical_name']}  vs.  {row['b_canonical_name']}",
            search_text=search_text,
            row=row,
        ))
    return entries


def _materialize_person_dup(entry: QueueIndexEntry) -> QueueItem:
    row = entry.row
    left = {"canonical_name": row["a_canonical_name"], "occupation": row["a_occupation"],
            "title": row["a_title"], "floruit_start": row["a_floruit_start"],
            "floruit_end": row["a_floruit_end"]}
    right = {"canonical_name": row["b_canonical_name"], "occupation": row["b_occupation"],
             "title": row["b_title"], "floruit_start": row["b_floruit_start"],
             "floruit_end": row["b_floruit_end"]}
    diff_rows = [
        ("Name", blank(left["canonical_name"]), blank(right["canonical_name"])),
        ("Source", row["a_source"], row["b_source"]),
        ("Occupation", blank(left["occupation"]), blank(right["occupation"])),
        ("Title", blank(left["title"]), blank(right["title"])),
        ("Floruit", f"{blank(left['floruit_start'])} -- {blank(left['floruit_end'])}",
                    f"{blank(right['floruit_start'])} -- {blank(right['floruit_end'])}"),
    ]
    return QueueItem(
        item_id=entry.item_id,
        item_type="person_dup",
        volume=None,
        header=f"{row['a_canonical_name']}  ({row['a_display_id']})  vs.  "
               f"{row['b_canonical_name']}  ({row['b_display_id']})",
        subheader=f"name_score={row['name_score']:.0f}  ·  dates={row['date_status']}  ·  "
                  f"{row['classification']} ({row['confidence']})",
        left_label=f"A · {row['a_display_id']}", right_label=f"B · {row['b_display_id']}",
        diff_rows=diff_rows,
        actions=[
            QueueAction("s", "same", "Same — flag only (s)"),
            QueueAction("m", "merge", "Same — merge now (m)", "primary"),
            QueueAction("d", "different", "Different (d)"),
            QueueAction("n", "next", "Next / not sure yet (n)"),
        ],
        payload={"candidate_pk": row["candidate_pk"], "person_a_pk": row["person_a_pk"],
                 "person_b_pk": row["person_b_pk"]},
        sort_score=entry.sort_score,
    )


# ── place_dup ─────────────────────────────────────────────────────────────

def _index_place_dup_items(volumes: list | None) -> list:
    entries = []
    df = db.get_place_duplicate_candidates(decision="")
    if volumes:
        df = df[df["source_volume"].isin(volumes)]
    for row in df.to_dict("records"):
        # Bulk to_dict("records") instead of .iterrows() + per-row .to_dict()
        # -- ~3x faster at real data scale (measured: 0.66s -> 0.23s for
        # person_dup's 15,862 rows), and this runs on every render since
        # build_queue_index() is called fresh every time.
        search_text = " ".join(str(row.get(k) or "") for k in (
            "place_canonical_name", "di_name", "display_id", "candidate_name",
            "di_place_type", "candidate_sysla", "di_region", "candidate_hreppur", "flag",
        )).lower()
        entries.append(QueueIndexEntry(
            item_id=f"place_dup:{row['candidate_pk']}",
            item_type="place_dup",
            volume=_to_int(row["source_volume"]),
            sort_score=row["name_score"] or 0,
            sort_name=f"{row['place_canonical_name']}  {row['candidate_name']}".lower(),
            list_label=f"{row['place_canonical_name']}  vs. nafnid: {row['candidate_name']}",
            search_text=search_text,
            row=row,
        ))
    return entries


def _materialize_place_dup(entry: QueueIndexEntry) -> QueueItem:
    row = entry.row
    diff_rows = [
        ("Name", blank(row.get("place_canonical_name") or row.get("di_name")), blank(row["candidate_name"])),
        ("Type / sysla", blank(row["di_place_type"]), blank(row["candidate_sysla"])),
        ("Region / hreppur", blank(row["di_region"]), blank(row["candidate_hreppur"])),
        ("Coordinates", "", f"{blank(row['candidate_lat'])}, {blank(row['candidate_lng'])}"),
    ]
    return QueueItem(
        item_id=entry.item_id,
        item_type="place_dup",
        volume=entry.volume,
        header=f"{row['place_canonical_name']}  ({row['display_id']})  vs.  nafnid: {row['candidate_name']}",
        subheader=f"name_score={row['name_score']:.1f}  ·  distance={blank(row['distance_km'])} km  ·  "
                  f"flag={row['flag'] or 'none'}",
        left_label="DI place", right_label="nafnid.is candidate",
        diff_rows=diff_rows,
        actions=[
            QueueAction("s", "same", "Same (s)", "primary"),
            QueueAction("d", "different", "Different (d)"),
            QueueAction("n", "next", "Next / not sure yet (n)"),
        ],
        payload={"candidate_pk": row["candidate_pk"]},
        sort_score=entry.sort_score,
    )


# ── review_item ───────────────────────────────────────────────────────────

def _index_review_items(volumes: list | None) -> list:
    entries = []
    for vn in (volumes or db.get_volumes()):
        df = db.get_open_review_items(vn)
        df = df[df["decision"].fillna("") == ""]
        for row in df.to_dict("records"):
            row["_volume"] = vn
            search_text = " ".join(str(row.get(k) or "") for k in (
                "extracted_name", "closest_match", "role_category", "role", "entity_type",
            )).lower()
            entries.append(QueueIndexEntry(
                item_id=f"review_item:{row['review_item_pk']}",
                item_type="review_item",
                volume=vn,
                sort_score=row["score"] or 0,
                sort_name=(row["extracted_name"] or "").lower(),
                list_label=f"{row['extracted_name']}  ({row['entity_type']})",
                search_text=search_text,
                row=row,
            ))
    return entries


def _materialize_review_item(entry: QueueIndexEntry) -> QueueItem:
    row = entry.row
    diff_rows = [
        ("Extracted name", blank(row["extracted_name"]), blank(row["closest_match"])),
        ("Role", blank(row["role_category"]), blank(row["role"])),
        ("Score", "", blank(row["score"])),
    ]
    return QueueItem(
        item_id=entry.item_id,
        item_type="review_item",
        volume=row["_volume"],
        header=f"{row['extracted_name']}  ({row['entity_type']})",
        subheader=f"Proposed match: {row['closest_match']}  (score {blank(row['score'])})",
        left_label="Extracted", right_label="Proposed match",
        diff_rows=diff_rows,
        actions=[
            QueueAction("a", "accept", "Accept — use proposed match (a)", "primary"),
            QueueAction("r", "reject", "Reject — treat as new entity (r)"),
            QueueAction("n", "next", "Next / not sure yet (n)"),
        ],
        payload={"review_item_pk": row["review_item_pk"]},
        sort_score=entry.sort_score,
    )


_INDEX_BUILDERS = {
    "new_person": _index_new_person_items,
    "new_place": _index_new_place_items,
    "person_dup": _index_person_dup_items,
    "place_dup": _index_place_dup_items,
    "review_item": _index_review_items,
}

_MATERIALIZERS = {
    "new_person": _materialize_new_person,
    "new_place": _materialize_new_place,
    "person_dup": _materialize_person_dup,
    "place_dup": _materialize_place_dup,
    "review_item": _materialize_review_item,
}


def build_queue_index(filt: QueueFilter) -> list:
    """Assembles the live, undecided-only queue for the given filter --
    cheap SQL-only fetch + filter/sort, no per-row fuzzy matching. Called
    fresh on every render (cheap at this data scale, and the only way an
    already-decided item reliably drops out immediately). Call materialize()
    on exactly the entry you're about to display."""
    entries = []
    for item_type in filt.item_types:
        if item_type == "new_person" and filt.flagged_only:
            entries.extend(_index_new_person_items(filt.volumes, flagged_only=True))
        else:
            entries.extend(_INDEX_BUILDERS[item_type](filt.volumes))

    if filt.search:
        needle = filt.search.strip().lower()
        entries = [e for e in entries if needle in e.search_text]

    if filt.sort == "score_desc":
        entries.sort(key=lambda e: e.sort_score, reverse=True)
    elif filt.sort == "score_asc":
        entries.sort(key=lambda e: e.sort_score)
    elif filt.sort == "name":
        entries.sort(key=lambda e: e.sort_name)
    # "default": item-type groups in filt.item_types iteration order, each
    # already sorted highest-confidence-first by its own db.py query.

    return entries


def materialize(entry: QueueIndexEntry) -> QueueItem:
    return _MATERIALIZERS[entry.item_type](entry)


def apply_action(item: QueueItem, action_key: str) -> dict:
    """Dispatches action_key to the right db.py mutator for item.item_type.
    "next" (available on every item) is a pure no-op -- the caller advances
    the queue position without this function mutating anything."""
    p = item.payload

    if action_key == "next":
        return {"action": "next"}

    if item.item_type == "new_person":
        # ok/add/skip also clear data_quality_flag -- the reviewer has just
        # decided whether this flagged row's data is fine (add/ok) or not
        # (skip), so the flag has served its purpose. Without this, the
        # flagged-only queue (db.get_persons(flagged_only=True), which has
        # no review_status condition at all) would keep matching this exact
        # row forever, making the action look like it silently did nothing.
        # merge needs no equivalent -- merge_into_authority already removes
        # the provisional row entirely.
        if action_key == "ok":
            db.update_person(p["pk"], review_status="ok", data_quality_flag="")
        elif action_key == "add":
            db.update_person(p["pk"], review_status="add", data_quality_flag="")
        elif action_key == "skip":
            db.update_person(p["pk"], review_status="skip", data_quality_flag="")
        elif action_key == "merge":
            db.merge_into_authority("person", p["pk"], p["match_pk"])
        else:
            raise ValueError(f"Unknown action {action_key!r} for new_person")
        return {"action": action_key, "pk": p["pk"]}

    if item.item_type == "new_place":
        if action_key == "ok":
            db.update_place(p["pk"], review_status="ok")
        elif action_key == "add":
            db.update_place(p["pk"], review_status="add")
        elif action_key == "skip":
            db.update_place(p["pk"], review_status="skip")
        elif action_key == "no_match":
            db.update_place(p["pk"], review_status="no_match")
        elif action_key == "merge":
            db.merge_into_authority("place", p["pk"], p["match_pk"])
        else:
            raise ValueError(f"Unknown action {action_key!r} for new_place")
        return {"action": action_key, "pk": p["pk"]}

    if item.item_type == "person_dup":
        if action_key == "same":
            db.record_person_duplicate_decision(p["candidate_pk"], "same")
        elif action_key == "different":
            db.record_person_duplicate_decision(p["candidate_pk"], "different")
        elif action_key == "merge":
            survivor, dropped_pk = sorted((p["person_a_pk"], p["person_b_pk"]))
            result = db.merge_persons(survivor, [dropped_pk])
            # Mark the decision too -- merge_persons already relinked this
            # candidate row's pks to the survivor on both sides, so leaving
            # decision blank would otherwise resurface it as a nonsensical
            # "duplicate of itself" pending item.
            db.record_person_duplicate_decision(p["candidate_pk"], "same")
            return {"action": action_key, "survivor_pk": survivor, **result}
        else:
            raise ValueError(f"Unknown action {action_key!r} for person_dup")
        return {"action": action_key, "candidate_pk": p["candidate_pk"]}

    if item.item_type == "place_dup":
        if action_key == "same":
            db.record_place_duplicate_decision(p["candidate_pk"], "same")
        elif action_key == "different":
            db.record_place_duplicate_decision(p["candidate_pk"], "different")
        else:
            raise ValueError(f"Unknown action {action_key!r} for place_dup")
        return {"action": action_key, "candidate_pk": p["candidate_pk"]}

    if item.item_type == "review_item":
        if action_key == "accept":
            db.set_review_decision(p["review_item_pk"], "accept")
            result = db.apply_review_decision(p["review_item_pk"])
        elif action_key == "reject":
            db.set_review_decision(p["review_item_pk"], "reject")
            result = db.apply_review_decision(p["review_item_pk"])
        else:
            raise ValueError(f"Unknown action {action_key!r} for review_item")
        return {"action": action_key, **result}

    raise ValueError(f"Unknown item_type {item.item_type!r}")


def apply_multi_merge(entries: list) -> dict:
    """Merges 2+ selected QueueIndexEntry objects together -- the List+detail
    view's cross-item counterpart to the single-item "merge" action. Deliberately
    takes cheap index entries, not materialized QueueItems: the pks needed here
    already live in entry.row (every index builder stores the full raw DB row),
    so this never needs materialize()'s db.search_authority() call at all.

    Survivor = lowest pk, matching the existing "oldest wins" convention
    db.merge_persons/merge_places already document."""
    if len(entries) < 2:
        raise ValueError("Need at least 2 entries to merge.")
    item_types = {e.item_type for e in entries}
    if len(item_types) > 1:
        raise ValueError(f"Can't merge a mix of item types: {sorted(item_types)}. "
                          "Select entries of only one type.")
    item_type = entries[0].item_type
    if item_type not in MERGEABLE_ITEM_TYPES:
        raise ValueError(f"{item_type!r} entries can't be merged with each other.")

    if item_type in ("new_person", "new_place"):
        pk_field = "person_pk" if item_type == "new_person" else "place_pk"
        pks = sorted(e.row[pk_field] for e in entries)
        survivor, dropped = pks[0], pks[1:]
        merge_fn = db.merge_persons if item_type == "new_person" else db.merge_places
        result = merge_fn(survivor, dropped)
        return {"item_type": item_type, "survivor_pk": survivor, **result}

    # person_dup: each entry is already a candidate PAIR, not a single pk --
    # union every pair's two pks first (dedupes correctly if selected pairs
    # overlap, e.g. A-B and B-C selected together), then merge same as above.
    pks = sorted({pk for e in entries for pk in (e.row["person_a_pk"], e.row["person_b_pk"])})
    survivor, dropped = pks[0], pks[1:]
    result = db.merge_persons(survivor, dropped)
    for e in entries:
        db.record_person_duplicate_decision(e.row["candidate_pk"], "same")
    return {"item_type": item_type, "survivor_pk": survivor, **result}
