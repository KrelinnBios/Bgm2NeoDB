import httpx
import pytest

from app.database import Database
from app.migrator import Migrator
from app.neodb import NeoDB
from app.resolution import choose, inspect_candidates, source_id, source_titles
from tests.conftest import catalog_item, collection, mark


@pytest.mark.asyncio
async def test_candidate_metadata_preserves_version_details_without_changing_identity():
    async with NeoDB("https://neo.example") as neo:
        data = {
            **catalog_item(),
            "season_number": 0,
            "episode_count": 12,
            "release_date": "2009-01-01",
            "director": ["导演"],
            "description": "版本简介",
            "actor": {"invalid": True},
        }
        item = neo.parse_item(data)
    assert item["uuid"] == data["uuid"]
    assert item["metadata"] == {
        "season_number": 0,
        "episode_count": 12,
        "release_date": "2009-01-01",
        "director": ["导演"],
        "description": "版本简介",
    }


def candidate(sid, title="作品", refs=()):
    return {
        **catalog_item(sid),
        "type": "Game",
        "title": title,
        "titles": [title],
        "external_resources": [{"url": url} for url in refs],
    }


@pytest.mark.asyncio
async def test_unique_shelved_candidate_still_requires_confirmation(engine, monkeypatch):
    engine.select_profile()
    source = collection(7, subject={"name_cn": "作品"})
    first, second = candidate(1), candidate(2)
    result = {"code": "ambiguous", "candidates": [first, second], "retryable": False}

    async def inspect(*args, **kwargs):
        engine._resolutions[7] = result
        return result

    class Neo:
        async def shelf(self, item, retry=False):
            pytest.fail("Unconfirmed candidates must not be selected by shelf membership")

    monkeypatch.setattr(engine, "inspect_entry", inspect)
    found = await engine.lookup_by_title(Neo(), source, force=True)
    assert found is None
    assert engine._resolutions[7]["code"] == "ambiguous"


@pytest.mark.parametrize("case", ["title", "source", "type", "incomplete", "ambiguous"])
async def test_shelved_search_hit_cannot_bypass_matching_rules(engine, server, case):
    server.sources = [collection(7, subject={"id": 7, "type": 4, "name": "作品"})]
    hit = candidate(1)
    if case == "title":
        hit = candidate(1, "无关作品")
    elif case == "source":
        hit = candidate(1, refs=["https://bgm.tv/subject/999"])
    elif case == "type":
        hit["type"] = "Movie"
    server.marks[hit["uuid"]] = mark(1, comment_text="Keep this unrelated comment")
    original = server.handler

    def handle(request):
        if request.url.path == "/api/catalog/search":
            return httpx.Response(
                200,
                json={
                    "pages": 9 if case == "incomplete" else 1,
                    "data": [hit, candidate(2)] if case == "ambiguous" else [hit],
                },
            )
        if request.url.path == "/api/catalog/fetch":
            return httpx.Response(404)
        return original(request)

    server.handler = handle
    engine.start("auto")
    await engine.task
    row = engine.db.rows(engine.pid)[0]
    assert row["status"] in {"conflict", "resolve_failed"}
    assert row["item"] is None
    assert row["resolution"]["code"] == (
        "incomplete" if case == "incomplete" else "ambiguous" if case == "ambiguous" else "no_match"
    )
    assert not server.writes
    assert server.marks[hit["uuid"]]["comment_text"] == "Keep this unrelated comment"
    assert not any("/shelf/item/" in request.url.path for request in server.requests)


async def test_unfinished_legacy_shelf_mapping_is_resolved_again(engine, server):
    engine.select_profile()
    engine.db.import_snapshot(engine.pid, "old", [collection(7)])
    engine.db.update(
        engine.pid,
        7,
        item=catalog_item(1),
        status="ready",
        resolution={"basis": "external_shelf", "code": "matched"},
    )
    engine.start("auto")
    await engine.task
    row = engine.db.rows(engine.pid)[0]
    assert row["status"] == "migrated"
    assert row["item"]["uuid"] == catalog_item(7)["uuid"]
    assert len(server.writes) == 1
    assert server.writes[0].url.path.endswith(catalog_item(7)["uuid"])


async def test_animation_search_uses_combined_category():
    def handle(request):
        assert request.url.params["category"] == "movie,tv"
        return httpx.Response(200, json={"pages": 1, "data": [catalog_item(1)]})

    async with NeoDB("https://neo.example", transport=httpx.MockTransport(handle)) as neo:
        result = await inspect_candidates(neo, collection(1))
    assert result["code"] == "matched"


def test_source_link_wins_over_another_title_match():
    linked = candidate(1, "Other title", ["https://bgm.tv/subject/7"])
    result = choose([candidate(2), linked], ["作品"], {"game"}, 7)
    assert result["item"] == linked
    assert result["basis"] == "source_url"


def test_excluding_one_duplicate_does_not_make_other_version_safe():
    first, second = candidate(1), candidate(2)
    result = choose([first, second], ["作品"], {"game"}, 7, [first["uuid"]])
    assert result["code"] == "ambiguous"
    assert not result["retryable"]


def test_known_different_bangumi_source_blocks_title_match():
    result = choose([candidate(1, refs=["https://bgm.tv/subject/8"])], ["作品"], {"game"}, 7)
    assert result["code"] == "no_match"


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.test/subject/7",
        "https://bgm.tv.evil.test/subject/7",
        "https://user@bgm.tv/subject/7",
        "https://bgm.tv/subject/x",
    ],
)
def test_source_link_validation(url):
    assert source_id(url) is None


def test_aliases_only_come_from_bangumi_alias_fields():
    detail = {
        "infobox": [
            {"key": "别名", "value": [{"v": "Alias"}, {"v": None}]},
            {"key": "简介", "value": "Not an alias"},
        ]
    }
    assert source_titles(collection(), detail)[-1] == "Alias"
    assert "Not an alias" not in source_titles(collection(), detail)


async def test_search_aggregates_pages_before_selecting():
    calls = []

    def handler(request):
        page = int(request.url.params.get("page", 1))
        calls.append(page)
        return httpx.Response(200, json={"pages": 2, "data": [candidate(page)]})

    async with NeoDB("https://neo.example", transport=httpx.MockTransport(handler)) as neo:
        result = await inspect_candidates(neo, collection(7, subject={"type": 4, "name": "作品"}))
    assert calls == [1, 2]
    assert result["code"] == "ambiguous"


async def test_truncated_results_cannot_be_auto_selected():
    async with NeoDB(
        "https://neo.example",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"pages": 9, "data": [candidate(1)]})
        ),
    ) as neo:
        result = await inspect_candidates(neo, collection(7, subject={"type": 4, "name": "作品"}))
    assert result["code"] == "incomplete"
    assert result["item"] is None


@pytest.fixture
async def engine(tmp_path, store, server):
    engine = Migrator(tmp_path, store, server.bgm, server.neo)
    yield engine
    await engine.close()


async def test_ambiguous_results_survive_next_auto_run(engine, server):
    server.sources = [collection(7, subject={"id": 7, "type": 4, "name": "作品"})]
    server.search_hits = [candidate(1), candidate(2)]
    engine.start("auto")
    await engine.task
    row = engine.db.rows(engine.pid)[0]
    assert row["status"] == "conflict"
    assert len(row["resolution"]["candidates"]) == 2
    count = len([r for r in server.requests if r.url.path == "/api/catalog/search"])
    engine.start("auto")
    await engine.task
    assert count == len([r for r in server.requests if r.url.path == "/api/catalog/search"])
    assert engine.db.rows(engine.pid)[0]["resolution"]["code"] == "ambiguous"
    assert not server.writes


async def test_bangumi_alias_enrichment_is_used(engine, server):
    server.sources = [collection(7, subject={"id": 7, "type": 4, "name": "原名"})]
    original = server.handler

    def handle(request):
        if request.url.path == "/v0/subjects/7":
            return httpx.Response(
                200, json={"id": 7, "infobox": [{"key": "别名", "value": "Alias"}]}
            )
        if request.url.path == "/api/catalog/search":
            return httpx.Response(
                200,
                json={
                    "data": [candidate(7, "Alias")]
                    if request.url.params["query"] == "Alias"
                    else []
                },
            )
        return original(request)

    server.handler = handle
    engine.start("auto")
    await engine.task
    row = engine.db.rows(engine.pid)[0]
    assert row["status"] == "migrated"
    assert row["subject_detail"]["id"] == 7
    assert row["resolution"]["basis"] == "exact_title"
    assert not any(r.url.path == "/api/catalog/fetch" for r in server.requests)


async def test_transient_search_error_is_not_no_match(engine, server):
    original = server.handler

    def handle(request):
        if request.url.path == "/api/catalog/search":
            return httpx.Response(503)
        return original(request)

    server.handler = handle
    engine.start("auto")
    await engine.task
    assert engine.db.rows(engine.pid)[0]["resolution"]["code"] == "transient"
    assert engine.db.rows(engine.pid)[0]["resolution"]["retryable"]
    server.handler = original
    engine.start("auto")
    await engine.task
    assert engine.report()["migrated"] == 1


def test_manual_mapping_survives_rescan_and_database_reopen(tmp_path):
    db = Database(tmp_path / "test.db")
    db.profile("profile", {})
    db.import_snapshot("profile", "first", [collection()])
    db.update(
        "profile", 1, item=catalog_item(), resolution={"basis": "manual"}, subject_detail={"id": 1}
    )
    db.import_snapshot("profile", "second", [collection()])
    row = Database(db.path).rows("profile")[0]
    assert row["resolution"]["basis"] == "manual"
    assert row["item"]["uuid"] == catalog_item()["uuid"]
    assert row["subject_detail"] is None


def test_target_lookup_and_single_row_reads_respect_profile_and_current_scan(tmp_path):
    db = Database(tmp_path / "lookup.db")
    uid = catalog_item(1)["uuid"]
    for pid in ("first", "second"):
        db.profile(pid, {})
        db.import_snapshot(pid, "old", [collection(1), collection(2)])
    db.update("first", 1, item=catalog_item(1), status="migrated")
    assert db.has_migrated_target("first", 2, uid)
    assert not db.has_migrated_target("first", 1, uid)
    assert not db.has_migrated_target("second", 2, uid)
    assert len(db.rows("first", subject_id=1)) == 1
    db.import_snapshot("first", "new", [collection(2)])
    assert not db.rows("first", subject_id=1)
    assert not db.has_migrated_target("first", 2, uid)
    db.update("second", 1, item=catalog_item(1), status="ready")
    assert not db.has_migrated_target("second", 2, uid)


async def test_season_url_uses_existing_api_without_fetch():
    calls = []

    def handle(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=catalog_item())

    async with NeoDB("https://neo.example", transport=httpx.MockTransport(handle)) as neo:
        await neo.item_from_url("https://neo.example/tv/season/" + catalog_item()["uuid"])
    assert calls == ["/api/tv/season/" + catalog_item()["uuid"]]
