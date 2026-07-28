"""Test fixtures for charter_pipeline's data layer.

Two DB fixtures, deliberately different in kind:

- `freshdb` -- an empty database built from schema.sql, seeded per-test with
  exactly the rows a test needs. Use this for anything about *behaviour*
  (constraint handling, merge semantics, propagation rules): the setup is
  visible in the test, so a failure points at the code rather than at some
  incidental property of the real corpus.
- `livedb` -- a copy of the real charter_pipeline.db. Use this only for
  characterisation: "does this mutator still do what it does today, at real
  data scale". Skips itself if the DB isn't on disk (it's gitignored, so a
  fresh clone has no copy).

Both point db.py's module-level DB_PATH/SNAPSHOT_DIR/UNDO_LOG_PATH at the
temp copy, so nothing here can touch the real database or the real
.snapshots/ directory.

The copy is made with sqlite3.Connection.backup(), NOT shutil.copy2 --
copy2 reads the main DB file only, so on a live database it can miss
committed data still sitting in a -wal, and once WAL is enabled it produces
torn copies. backup() takes a consistent snapshot of a live DB.
"""
import sqlite3
import sys
from pathlib import Path

import pytest

PKG_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG_DIR))

import db as db_module  # noqa: E402

LIVE_DB = PKG_DIR / "charter_pipeline.db"


def _redirect_db(path: Path, tmp_path: Path, monkeypatch) -> None:
    """Repoint db.py at the temp copy.

    Only DB_PATH needs setting: the undo machinery resolves its snapshot
    directory from DB_PATH at call time, so snapshots follow the database
    automatically. (They used to be anchored to db.py's own directory, which
    meant a redirected DB_PATH wrote snapshots of the temp database into the
    REAL .snapshots/ -- see db._snapshot_dir.)
    """
    monkeypatch.setattr(db_module, "DB_PATH", path)
    # Module-level cache survives across tests in one process otherwise.
    db_module.invalidate_authority_cache()


@pytest.fixture
def freshdb(tmp_path, monkeypatch):
    path = tmp_path / "fresh.db"
    db_module.init_db(path)
    _redirect_db(path, tmp_path, monkeypatch)
    yield path
    db_module.invalidate_authority_cache()


@pytest.fixture
def livedb(tmp_path, monkeypatch):
    if not LIVE_DB.exists():
        pytest.skip(f"live database not present at {LIVE_DB}")
    path = tmp_path / "live.db"
    src = sqlite3.connect(str(LIVE_DB))
    dst = sqlite3.connect(str(path))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    _redirect_db(path, tmp_path, monkeypatch)
    yield path
    db_module.invalidate_authority_cache()


@pytest.fixture
def db():
    """The module under test, so tests read `db.merge_persons(...)`."""
    return db_module


# ---------------------------------------------------------------------------
# Seed helpers -- raw SQL on purpose, so fixtures never depend on the
# mutators being tested.
# ---------------------------------------------------------------------------

@pytest.fixture
def seed():
    class Seeder:
        def __init__(self):
            self._n = 0

        def _conn(self):
            return db_module.get_connection()

        def person(self, canonical_name, *, volume=1, status="provisional",
                   floruit_start=None, floruit_end=None, **cols) -> int:
            self._n += 1
            fields = {
                "display_id": cols.pop("display_id", f"v{volume:02d}-p{self._n:04d}"),
                "legacy_id": cols.pop("legacy_id", f"p{self._n:03d}"),
                "source_volume": volume,
                "status": status,
                "canonical_name": canonical_name,
                "floruit_start": floruit_start,
                "floruit_end": floruit_end,
                **cols,
            }
            names = ", ".join(fields)
            marks = ", ".join("?" for _ in fields)
            conn = self._conn()
            try:
                with conn:
                    cur = conn.execute(
                        f"INSERT INTO persons ({names}) VALUES ({marks})",
                        tuple(fields.values()),
                    )
                    return cur.lastrowid
            finally:
                conn.close()

        def place(self, canonical_name, *, volume=1, status="provisional", **cols) -> int:
            self._n += 1
            fields = {
                "display_id": cols.pop("display_id", f"v{volume:02d}-l{self._n:04d}"),
                "legacy_id": cols.pop("legacy_id", f"l{self._n:03d}"),
                "source_volume": volume,
                "status": status,
                "canonical_name": canonical_name,
                **cols,
            }
            names = ", ".join(fields)
            marks = ", ".join("?" for _ in fields)
            conn = self._conn()
            try:
                with conn:
                    cur = conn.execute(
                        f"INSERT INTO places ({names}) VALUES ({marks})",
                        tuple(fields.values()),
                    )
                    return cur.lastrowid
            finally:
                conn.close()

        def person_pair(self, a_pk, b_pk, *, name_score=95.0, decision="",
                        classification="likely_duplicate", **cols) -> int:
            a, b = sorted((a_pk, b_pk))
            conn = self._conn()
            try:
                with conn:
                    cur = conn.execute(
                        """INSERT INTO person_duplicate_candidates
                           (person_a_pk, person_b_pk, name_score, decision,
                            classification, date_status, confidence)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (a, b, name_score, decision, classification,
                         cols.get("date_status", "overlap"),
                         cols.get("confidence", "high")),
                    )
                    return cur.lastrowid
            finally:
                conn.close()

        def place_candidate(self, place_pk, *, rank=1, name_score=95.0,
                            nafnid="", lat=None, lng=None, decision="", **cols) -> int:
            conn = self._conn()
            try:
                with conn:
                    cur = conn.execute(
                        """INSERT INTO place_duplicate_candidates
                           (place_pk, di_name, candidate_rank, name_score,
                            candidate_name, candidate_nafnid, candidate_lat,
                            candidate_lng, decision, di_sysla_given, candidate_sysla,
                            match_sources, wikidata_status)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (place_pk, cols.get("di_name", ""), rank, name_score,
                         cols.get("candidate_name", ""), nafnid, lat, lng, decision,
                         cols.get("di_sysla_given", ""), cols.get("candidate_sysla", ""),
                         cols.get("match_sources", "name"),
                         cols.get("wikidata_status", "ungeocoded")),
                    )
                    return cur.lastrowid
            finally:
                conn.close()

        def charter(self, *, volume=1, sequence=1, **cols) -> int:
            fields = {
                "charter_id_placeholder": cols.pop(
                    "charter_id_placeholder", f"DI_{volume:02d}_{sequence:04d}"),
                "volume": volume,
                "sequence": sequence,
                **cols,
            }
            names = ", ".join(fields)
            marks = ", ".join("?" for _ in fields)
            conn = self._conn()
            try:
                with conn:
                    cur = conn.execute(
                        f"INSERT INTO charters ({names}) VALUES ({marks})",
                        tuple(fields.values()),
                    )
                    return cur.lastrowid
            finally:
                conn.close()

        def charter_person(self, charter_pk, person_pk, **cols) -> int:
            conn = self._conn()
            try:
                with conn:
                    cur = conn.execute(
                        """INSERT INTO charter_persons
                           (charter_pk, person_pk, ordinal, role_category, extracted_name)
                           VALUES (?, ?, ?, ?, ?)""",
                        (charter_pk, person_pk, cols.get("ordinal", 1),
                         cols.get("role_category", "witness-testimony"),
                         cols.get("extracted_name", "")),
                    )
                    return cur.lastrowid
            finally:
                conn.close()

    return Seeder()


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def query():
    def _query(sql, params=()):
        conn = db_module.get_connection()
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()
    return _query


@pytest.fixture
def scalar(query):
    def _scalar(sql, params=()):
        rows = query(sql, params)
        if not rows:
            return None
        return next(iter(rows[0].values()))
    return _scalar
