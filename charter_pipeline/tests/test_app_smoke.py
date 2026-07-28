"""Whole-app smoke test, plus the database-path guard.

Runs review_app.py through Streamlit's real script runner against a copy of
the live database. There are no unit tests for the app's own layout, and the
grids are canvas-rendered so they can't be click-tested through a browser --
this is the cheapest thing that catches "the app doesn't start", which is
otherwise only discoverable by launching it and looking.

Isolation note: AppTest runs the script in *this* process, so `import config`
inside the app returns the already-cached module and its import-time
`config.DB_PATH`. Setting the DB_PATH environment variable here therefore does
NOT redirect the app (an earlier version of this file did that and silently
tested against the real database). Everything the app queries goes through
`db.*`, so the `livedb` fixture's monkeypatch of `db.DB_PATH` is what actually
isolates it.
"""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

PKG_DIR = Path(__file__).resolve().parent.parent
APP = PKG_DIR / "review_app.py"


def _run_app():
    return AppTest.from_file(str(APP), default_timeout=90).run()


def test_app_starts_without_exception(livedb):
    at = _run_app()
    assert not at.exception, f"app raised on startup: {at.exception}"


def test_app_renders_a_substantial_tree(livedb):
    at = _run_app()
    assert not at.exception
    assert len(at.markdown) + len(at.caption) > 3


def test_app_reports_the_database_it_actually_opened(livedb):
    at = _run_app()
    captions = " ".join(c.value for c in at.caption)
    assert Path(livedb).name in captions, captions


def test_app_stops_with_an_error_on_an_empty_database(tmp_path, monkeypatch, db):
    """The footgun this guard exists for: sqlite3.connect() creates an empty
    file for a path that doesn't exist, so a wrong DB_PATH used to surface as
    `no such table: places` from deep in a traceback -- or as an app that
    renders perfectly and shows nothing, indistinguishable from "all done"."""
    empty = tmp_path / "empty.db"
    empty.touch()
    monkeypatch.setattr(db, "DB_PATH", empty)

    at = _run_app()

    assert not at.exception, "should stop cleanly, not raise"
    errors = " ".join(e.value for e in at.error)
    assert "empty.db" in errors, f"error must name the resolved path; got: {errors}"


# ---------------------------------------------------------------------------
# check_database() on its own
# ---------------------------------------------------------------------------

def test_check_database_passes_on_a_real_database(livedb, db):
    assert db.check_database(livedb) is None


def test_check_database_reports_a_nonexistent_path(tmp_path, db):
    problem = db.check_database(tmp_path / "nope.db")
    assert problem is not None
    assert "nope.db" in problem


def test_check_database_reports_an_empty_file_as_such(tmp_path, db):
    empty = tmp_path / "blank.db"
    empty.touch()
    problem = db.check_database(empty)
    assert problem is not None
    assert "blank.db" in problem
    assert "empty" in problem.lower()
    assert "persons" in problem


def test_check_database_reports_a_partial_schema(tmp_path, db):
    import sqlite3
    partial = tmp_path / "partial.db"
    conn = sqlite3.connect(partial)
    conn.execute("CREATE TABLE persons (person_pk INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    problem = db.check_database(partial)
    assert problem is not None
    # Parse the list rather than substring-matching it: "persons" is a
    # substring of "charter_persons", so `in` would give a false positive.
    listed = {t.strip(" .") for t in
              problem.split("missing table(s)")[-1].split(",")}
    assert "places" in listed
    assert "persons" not in listed          # the one table that does exist
    assert "charter_persons" in listed


def test_check_database_mentions_the_env_override_when_set(tmp_path, monkeypatch, db):
    """A stale DB_PATH in the environment silently redirecting the whole app
    has already happened once; the message should point at it."""
    monkeypatch.setenv("DB_PATH", "/somewhere/stale/test.db")
    problem = db.check_database(tmp_path / "nope.db")
    assert "DB_PATH" in problem
    assert "/somewhere/stale/test.db" in problem


def test_check_database_never_raises_on_a_junk_file(tmp_path, db):
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"this is not a database at all, not even close")
    problem = db.check_database(junk)
    assert isinstance(problem, str) and problem


def test_check_database_does_not_create_a_missing_file(tmp_path, db):
    """Calling the guard must not itself produce the empty file it warns about."""
    missing = tmp_path / "absent.db"
    db.check_database(missing)
    assert not missing.exists()
