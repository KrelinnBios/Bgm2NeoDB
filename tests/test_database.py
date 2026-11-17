import pytest

from app.database import Database, encode
from tests.conftest import catalog_item, collection


@pytest.fixture
def database(tmp_path):
    db = Database(tmp_path / "migration.db")
    db.profile("current", {})
    db.import_snapshot("current", "old", [collection(999)])
    db.import_snapshot("current", "new", [collection(sid) for sid in range(65, 0, -1)])
    db.profile("other", {})
    db.import_snapshot("other", "new", [collection(1000)])
    return db


@pytest.mark.parametrize(
    ("page", "ids"),
    [
        (0, range(1, 31)),
        (1, range(1, 31)),
        (2, range(31, 61)),
        (3, range(61, 66)),
        (4, []),
        (10**30, []),
    ],
)
def test_page_only_decodes_visible_rows(database, monkeypatch, page, ids):
    decoded = []
    original = database._decode_rows

    def decode(rows):
        decoded.extend(row["subject_id"] for row in rows)
        return original(rows)

    monkeypatch.setattr(database, "_decode_rows", decode)
    total, rows = database.page("current", page=page)
    assert total == 65
    assert [row["subject_id"] for row in rows] == list(ids)
    assert decoded == list(ids)
    assert rows == [database.rows("current", subject_id=sid)[0] for sid in ids]


def test_page_counts_filtered_rows_and_preserves_unknown_filter_behavior(database):
    for sid in range(1, 66, 2):
        database.update("current", sid, status="failed")
    total, rows = database.page("current", page=2, statuses={"failed", "partial"})
    assert total == 33
    assert [row["subject_id"] for row in rows] == [61, 63, 65]
    assert database.page("current", statuses=set()) == (0, [])
    assert database.page("current", statuses={"unknown"}) == (0, [])
    assert database.page("missing") == (0, [])


@pytest.mark.parametrize(
    ("query", "ids"),
    [
        ("STRASSE", [1]),
        ("σ", [2]),
        ("作品", [3]),
        ("%_", [1]),
        ("' OR 1=1 --", []),
        ("44", [4]),
        ("", [1, 2, 3, 4]),
    ],
)
def test_search_keeps_unicode_literal_and_title_fallback_semantics(database, query, ids):
    database.import_snapshot(
        "current",
        "search",
        [
            collection(1, subject={"name_cn": "Straße %_", "name": "hidden"}),
            collection(2, subject={"name_cn": "", "name": "ς"}),
            collection(3, subject={"name_cn": None, "name": "作品"}),
            collection(4, subject=None),
        ],
    )
    total, rows = database.page("current", query=query)
    assert total == len(ids)
    assert [row["subject_id"] for row in rows] == ids
    assert database.page("current", query="hidden") == (0, [])
    database.update("current", 1, status="failed")
    assert database.page("current", statuses={"pending"}, query="STRASSE") == (0, [])


def test_report_does_not_decode_large_detail_payloads(database):
    detail = "large metadata " * 10000
    database.import_snapshot(
        "current", "large", [collection(1, subject={"name": "title", "summary": detail})]
    )
    database.update(
        "current",
        1,
        item={**catalog_item(), "description": detail},
        plan={"before": {"comment_text": detail}},
        subject_detail={"summary": detail},
        resolution={"retryable": False, "candidates": [{"description": detail}]},
    )
    rows = database.report_rows("current")
    assert len(rows) == 1
    assert rows[0]["item"] and rows[0]["plan"]
    assert rows[0]["resolution"] == {"retryable": False}
    assert len(encode(rows)) < 1000


def test_unchanged_profile_does_not_write_metadata(database):
    with database.connect() as db:
        db.executescript("""
            CREATE TABLE profile_updates (id TEXT);
            CREATE TRIGGER record_profile_updates AFTER UPDATE ON profiles BEGIN
                INSERT INTO profile_updates VALUES (NEW.id);
            END;
        """)
    before = database.profile("current")
    assert database.profile("current", {}) == before
    with database.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM profile_updates").fetchone()[0] == 0
    changed = database.profile("current", {"nickname": "updated"})
    assert changed == {**before, "metadata": encode({"nickname": "updated"})}
    with database.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM profile_updates").fetchone()[0] == 1
