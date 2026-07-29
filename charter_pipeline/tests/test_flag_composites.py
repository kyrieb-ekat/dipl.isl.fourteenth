"""The 10_flag_composite_persons.py CLI.

Run as a subprocess with DB_PATH pointed at a copy, because the script
resolves its database through config at import time -- the same reason the
app smoke test can't redirect it in-process.
"""
import subprocess
import sys
from pathlib import Path

import pytest

PKG_DIR = Path(__file__).resolve().parent.parent
SCRIPT = PKG_DIR / "10_flag_composite_persons.py"


def _run(db_path, *args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=str(PKG_DIR), capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "DB_PATH": str(db_path),
             "HOME": str(Path.home())},
    )


@pytest.fixture
def flagged_count(query):
    def _count():
        return query("SELECT COUNT(*) AS n FROM persons "
                     "WHERE data_quality_flag LIKE '%composite_record%'")[0]["n"]
    return _count


def test_dry_run_writes_nothing(livedb, flagged_count):
    before = flagged_count()
    result = _run(livedb)
    assert result.returncode == 0, result.stderr
    assert "Dry run only" in result.stdout
    assert flagged_count() == before


def test_dry_run_reports_the_known_composites(livedb):
    out = _run(livedb).stdout
    assert "p027" in out
    assert "1180-1488" in out
    assert "more than one lifetime" in out
    # the saint/human conflation, which span alone cannot catch
    assert "p006" in out or "saint" in out


def test_confirm_flags_the_certain_records(livedb, flagged_count, query, db):
    """Asserts a delta rather than an absolute count: the live database may
    already have been flagged, so `livedb` copies it in that state."""
    conn = db.get_connection()
    with conn:
        conn.execute("UPDATE persons SET data_quality_flag = "
                     "REPLACE(REPLACE(data_quality_flag, 'composite_record;', ''), "
                     "'composite_record', '')")
    conn.close()
    db.invalidate_authority_cache()
    assert flagged_count() == 0

    result = _run(livedb, "--confirm")
    assert result.returncode == 0, result.stderr

    n = flagged_count()
    assert n >= 10
    assert f"Flagged {n} person(s)" in result.stdout


def test_confirm_is_idempotent(livedb, flagged_count):
    _run(livedb, "--confirm")
    first = flagged_count()
    second = _run(livedb, "--confirm")
    assert "Flagged 0 person(s)" in second.stdout
    assert flagged_count() == first


def test_existing_flag_values_are_preserved(livedb, db, query, scalar):
    """A record can be both a later-transmission actor and a composite."""
    pk = scalar("SELECT person_pk FROM persons WHERE display_id = 'p027'")
    db.add_data_quality_flag(pk, "later_transmission_actor")

    _run(livedb, "--confirm")

    flag = query("SELECT data_quality_flag FROM persons WHERE person_pk = ?",
                 (pk,))[0]["data_quality_flag"]
    assert "later_transmission_actor" in flag
    assert "composite_record" in flag


def test_severity_review_includes_more_than_certain(livedb):
    certain_only = _run(livedb).stdout
    with_review = _run(livedb, "--severity", "review").stdout
    assert with_review.count("[certain]") == certain_only.count("[certain]")
    assert with_review.count("[ review]") > 0


def test_it_reports_a_bad_database_instead_of_crashing(tmp_path):
    empty = tmp_path / "empty.db"
    empty.touch()
    result = _run(empty)
    assert result.returncode == 1
    assert "Database problem" in result.stdout
    assert "empty.db" in result.stdout


def test_status_and_review_status_are_never_touched(livedb, query):
    before = query("SELECT person_pk, status, review_status FROM persons "
                   "ORDER BY person_pk")
    _run(livedb, "--confirm", "--severity", "review")
    after = query("SELECT person_pk, status, review_status FROM persons "
                  "ORDER BY person_pk")
    assert before == after


def test_nothing_is_deleted(livedb, scalar):
    before = scalar("SELECT COUNT(*) FROM persons")
    _run(livedb, "--confirm", "--severity", "review")
    assert scalar("SELECT COUNT(*) FROM persons") == before
