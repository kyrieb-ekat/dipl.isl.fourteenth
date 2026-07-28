"""Undo snapshots must live beside the database they are snapshots OF.

Regression test for an observed near-miss: SNAPSHOT_DIR was anchored to
db.py's own directory while DB_PATH is environment-overridable, so pointing
DB_PATH at a scratch database still wrote that database's undo snapshots into
the LIVE .snapshots/ directory, as the newest entry in the live undo log. One
Undo click in a normal session would then have restored the scratch database
over the real one.
"""
from pathlib import Path


def test_snapshot_dir_follows_the_database(freshdb, db, seed):
    a = seed.person("Jón Sigurðsson")
    b = seed.person("Jon Sigurdsson")

    db.merge_persons(a, [b])

    beside_db = Path(freshdb).parent / ".snapshots"
    assert beside_db.exists(), "snapshot dir should be created next to the DB"
    assert list(beside_db.glob("*.db")), "a snapshot should have been written there"


def test_no_snapshot_leaks_into_the_package_directory(freshdb, db, seed):
    """The specific contamination: nothing may be written to the .snapshots/
    dir next to db.py when DB_PATH points elsewhere."""
    pkg_snapshots = Path(db.__file__).parent / ".snapshots"
    before = {p.name for p in pkg_snapshots.glob("*.db")} if pkg_snapshots.exists() else set()

    a = seed.person("Jón Sigurðsson")
    b = seed.person("Jon Sigurdsson")
    db.merge_persons(a, [b])

    after = {p.name for p in pkg_snapshots.glob("*.db")} if pkg_snapshots.exists() else set()
    assert after == before, f"leaked snapshots into the package dir: {after - before}"


def test_undo_log_is_scoped_to_the_database(freshdb, db, seed):
    a = seed.person("Jón Sigurðsson")
    b = seed.person("Jon Sigurdsson")
    db.merge_persons(a, [b])

    log = Path(freshdb).parent / ".snapshots" / "undo_log.json"
    assert log.exists()
    last = db.get_last_action()
    assert last and "person" in last["description"]


def test_undo_restores_only_this_database(freshdb, db, seed, scalar):
    a = seed.person("Jón Sigurðsson")
    b = seed.person("Jon Sigurdsson")
    assert scalar("SELECT COUNT(*) FROM persons") == 2

    db.merge_persons(a, [b])
    assert scalar("SELECT COUNT(*) FROM persons") == 1

    db.undo_last_action()
    assert scalar("SELECT COUNT(*) FROM persons") == 2


def test_two_databases_keep_separate_undo_stacks(tmp_path, monkeypatch, db):
    """Two DB_PATHs must not share an undo stack -- that sharing is exactly
    what allowed a snapshot of one to be restored over the other."""
    first, second = tmp_path / "one" / "a.db", tmp_path / "two" / "b.db"
    for p in (first, second):
        p.parent.mkdir(parents=True, exist_ok=True)
        db.init_db(p)

    def seed_two(path):
        monkeypatch.setattr(db, "DB_PATH", path)
        db.invalidate_authority_cache()
        conn = db.get_connection()
        with conn:
            for i, name in enumerate(("A", "B"), start=1):
                conn.execute(
                    "INSERT INTO persons (display_id, legacy_id, canonical_name) "
                    "VALUES (?, ?, ?)", (f"{path.stem}-{i}", f"p{i}", name))
            pks = [r[0] for r in conn.execute("SELECT person_pk FROM persons ORDER BY person_pk")]
        conn.close()
        return pks

    pks_a = seed_two(first)
    monkeypatch.setattr(db, "DB_PATH", first)
    db.merge_persons(pks_a[0], [pks_a[1]])

    pks_b = seed_two(second)
    monkeypatch.setattr(db, "DB_PATH", second)
    db.merge_persons(pks_b[0], [pks_b[1]])

    logs = [Path(p).parent / ".snapshots" / "undo_log.json" for p in (first, second)]
    assert all(f.exists() for f in logs)
    # One entry each, not two in a shared stack.
    import json
    assert [len(json.loads(f.read_text())) for f in logs] == [1, 1]
