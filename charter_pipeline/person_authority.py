"""
In-memory index over the canonical persons table in charter_pipeline.db.

Same public interface as before the SQLite migration (PersonEntry,
PersonAuthority with .entries/.lookup()/.lookup_wikidata()/.find(),
split_variants()) so 03_resolve_entities.py and any other existing caller
needs no changes -- only the backing store moved from
person_names_authority.csv to the `persons` table (status='canonical').
"""

import sys
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import db
from db import _PAREN_TAIL, split_variants  # re-exported for backward compatibility


def _int_or_blank(v) -> str:
    """DataFrame-sourced nullable-int columns come back as float64 with NaN
    for NULL (pandas has no nullable-int dtype by default) -- str(v) on a
    real NaN gives the literal string 'nan', and on a real value gives
    '1340.0' instead of '1340'. Route every floruit_start/end read through
    this instead of a bare str()."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return str(int(v))

AUTHORITY_PATH = Path(__file__).parent / "person_names_authority.csv"  # historical, unused post-migration


@dataclass
class PersonEntry:
    person_id:      str   # display_id
    canonical_name: str
    wikidata_id:    str = ""
    variants:       list[str] = field(default_factory=list)
    patronymic:     str = ""
    occupation:     str = ""
    title:          str = ""
    floruit_start:  str = ""
    floruit_end:    str = ""
    gender:         str = ""
    notes:          str = ""
    person_pk:      int | None = None

    def all_names(self) -> list[str]:
        return db._all_names(self.canonical_name, ";".join(self.variants))


def _row_to_entry(row: dict) -> PersonEntry:
    return PersonEntry(
        person_id=row["display_id"],
        canonical_name=row["canonical_name"],
        wikidata_id=row.get("wikidata_id") or "",
        variants=split_variants(row.get("variant_names", "")),
        patronymic=row.get("patronymic") or "",
        occupation=row.get("occupation") or "",
        title=row.get("title") or "",
        floruit_start=_int_or_blank(row.get("floruit_start")),
        floruit_end=_int_or_blank(row.get("floruit_end")),
        gender=row.get("gender") or "",
        notes=row.get("notes") or "",
        person_pk=row.get("person_pk"),
    )


class PersonAuthority:
    """In-memory index over persons WHERE status='canonical'. Lookup is
    exact (case-insensitive) on canonical_name and all variant forms."""

    def __init__(self, path: Path | None = None):
        # `path` accepted for signature backward-compatibility; ignored --
        # the SQLite DB (config.DB_PATH) is the only source now.
        df = db.get_persons(status="canonical")
        self.entries: list[PersonEntry] = [_row_to_entry(r) for r in df.to_dict("records")]
        self._name_index: dict[str, PersonEntry] = {}
        self._wikidata_index: dict[str, PersonEntry] = {}
        for entry in self.entries:
            for name in entry.all_names():
                if name and name not in self._name_index:
                    self._name_index[name] = entry
            if entry.wikidata_id and entry.wikidata_id not in self._wikidata_index:
                self._wikidata_index[entry.wikidata_id] = entry
        print(f"[person_authority] Loaded {len(self.entries)} entries, "
              f"{len(self._name_index)} name forms, "
              f"{len(self._wikidata_index)} Wikidata QIDs.")

    def lookup(self, name: str) -> PersonEntry | None:
        return self._name_index.get((name or "").strip().lower())

    def lookup_wikidata(self, qid: str) -> PersonEntry | None:
        return self._wikidata_index.get((qid or "").strip())

    def find(self, canonical_name: str, wikidata_id: str = "",
             variant_names: list[str] | None = None) -> PersonEntry | None:
        entry = self.lookup(canonical_name)
        if entry:
            return entry
        canonical_stripped = _PAREN_TAIL.sub("", canonical_name).strip()
        if canonical_stripped and canonical_stripped != canonical_name:
            entry = self.lookup(canonical_stripped)
            if entry:
                return entry
        if wikidata_id:
            entry = self.lookup_wikidata(wikidata_id)
            if entry:
                return entry
        for v in (variant_names or []):
            entry = self.lookup(v)
            if entry:
                return entry
        return None

    def __len__(self):
        return len(self.entries)
