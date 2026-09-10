import httpx
import pytest

from app.auth import CredentialStore
from app.config import ORIGIN
from app.routes import create_app
from tests.conftest import MemoryKeyring, catalog_item, collection, mark


@pytest.fixture
async def web(tmp_path, server):
    app = create_app(tmp_path, CredentialStore(backend=MemoryKeyring()), server.bgm, server.neo)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN
        ) as client:
            yield client, app


async def test_home_and_security_headers(web):
    client, _ = web
    response = await client.get("/")
    assert response.status_code == 200
    assert "Bangumi → NeoDB 迁移工具" in response.text
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "HttpOnly" in response.headers["set-cookie"]
    assert (await client.get("/api/state")).status_code == 200


async def test_no_session_no_access(web):
    client, _ = web
    assert (await client.get("/api/state")).status_code == 403


async def test_dns_rebinding_host_blocked(web):
    client, _ = web
    assert (await client.get("/", headers={"Host": "evil.example"})).status_code == 403


async def test_cross_site_writes_blocked(web):
    client, _ = web
    await client.get("/")
    response = await client.post(
        "/api/jobs/migrate", json={}, headers={"Origin": "https://evil.example"}
    )
    assert response.status_code == 403


async def test_validation_does_not_echo_token(web):
    client, _ = web
    page = await client.get("/")
    import re

    csrf = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
    response = await client.post(
        "/api/connect/bangumi",
        json={"token": {"secret": "DO-NOT-ECHO"}},
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
    )
    assert response.status_code == 422
    assert "DO-NOT-ECHO" not in response.text


async def test_connect_scan_preview_migrate_web_workflow(web):
    import re

    client, app = web
    page = await client.get("/")
    csrf = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
    headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
    assert (
        await client.post("/api/connect/bangumi", json={"token": "secret"}, headers=headers)
    ).status_code == 200
    app.state.engine.credentials.put(
        "neodb",
        {
            "token": "secret",
            "instance": "https://neo.example",
            "user": {"url": "https://neo.example/users/user/"},
        },
    )
    assert (await client.post("/api/jobs/scan", json={}, headers=headers)).status_code == 200
    await app.state.engine.task
    entries = (await client.get("/api/entries")).json()
    assert entries["entries"][0]["status"] == "migrated"
    assert (await client.post("/api/jobs/migrate", json={}, headers=headers)).status_code == 200
    await app.state.engine.task
    assert (await client.get("/api/state")).json()["summary"]["migrated"] == 1
    assert (await client.get("/api/export/report")).status_code == 404
    assert (await client.get("/api/export/failed")).status_code == 404
    assert (await client.get("/api/export/source")).status_code == 404


async def test_each_status_has_separate_count_and_filter(web):
    client, app = web
    await client.get("/")
    engine = app.state.engine
    engine.pid = "status-test"
    engine.db.profile(engine.pid, {})
    statuses = (
        "pending",
        "ready",
        "writing",
        "migrated",
        "skipped",
        "resolve_failed",
        "failed",
        "partial",
        "conflict",
        "blocked_private_visibility",
    )
    engine.db.import_snapshot(
        engine.pid, "scan", [collection(sid) for sid in range(1, len(statuses) + 1)]
    )
    for sid, status in enumerate(statuses, 1):
        engine.db.update(engine.pid, sid, status=status)
    summary = engine.report()
    assert sum(summary[status] for status in statuses) == summary["total"] == 10
    for sid, status in enumerate(statuses, 1):
        result = (await client.get("/api/entries", params={"filter": f"status:{status}"})).json()
        assert result["total"] == summary[status] == 1
        assert result["entries"][0]["id"] == sid
    # Keep legacy aggregate filters available to existing clients.
    assert (await client.get("/api/entries?filter=failed")).json()["total"] == 6
    assert (await client.get("/api/entries?filter=done")).json()["total"] == 1
    assert (await client.get("/api/entries?filter=group:queued")).json()["total"] == 4
    assert (await client.get("/api/entries?filter=group:failed")).json()["total"] == 2
    assert (await client.get("/api/entries?filter=group:migrated")).json()["total"] == 1
    engine.db.update(engine.pid, 3, status="migrated")
    assert engine.report()["writing"] == 0


def test_keyring_failure_does_not_fall_back_to_plaintext():
    class Broken:
        def set_password(self, *args):
            raise RuntimeError("secret")

    store = CredentialStore(backend=Broken())
    store.put("bangumi", {"token": "secret"})
    assert store.get("bangumi") == {"token": "secret"}
    assert "仅在本次运行" in store.warning


async def test_map_candidates_and_manual_mapping(web, server):
    import re

    client, app = web
    await client.get("/")
    engine = app.state.engine
    engine.credentials.put(
        "neodb",
        {
            "token": "NEO-SECRET",
            "instance": "https://neo.example",
            "user": {"url": "https://neo.example/users/user/"},
        },
    )
    engine.pid = "map-test"
    engine.db.profile(engine.pid, {})
    engine.db.import_snapshot(engine.pid, "scan", [collection(1)])
    engine.db.update(engine.pid, 1, status="resolve_failed", stage="resolve", error="需要手动匹配")
    server.search_hits = [catalog_item(1)]

    candidates = (await client.get("/api/map/candidates/1")).json()
    assert candidates["candidates"]
    assert candidates["exact"] == catalog_item(1)["url"]

    page = await client.get("/")
    csrf = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
    headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
    response = await client.post(
        "/api/map/1", json={"url": candidates["candidates"][0]["url"]}, headers=headers
    )
    assert response.status_code == 200
    row = engine.db.rows(engine.pid)[0]
    assert row["status"] == "pending"
    assert row["item"]["uuid"] == catalog_item(1)["uuid"]


async def test_manual_mapping_uses_selected_target_even_when_already_migrated(web, server):
    import re

    client, app = web
    await client.get("/")
    engine = app.state.engine
    engine.credentials.put(
        "bangumi",
        {"token": "BGM-SECRET", "user": {"id": 100, "username": "bgm-user"}},
    )
    engine.credentials.put(
        "neodb",
        {
            "token": "NEO-SECRET",
            "instance": "https://neo.example",
            "user": {"url": "https://neo.example/users/user/"},
        },
    )
    engine.select_profile()
    engine.db.profile(engine.pid, {})
    engine.db.import_snapshot(
        engine.pid,
        "scan",
        [collection(1), collection(2, type=1, rate=6, comment="Selected collection")],
    )
    engine.db.update(engine.pid, 1, item=catalog_item(1), status="migrated")
    engine.db.update(engine.pid, 2, status="conflict", stage="mapping")
    server.marks[catalog_item(1)["uuid"]] = mark(
        1, shelf_type="complete", rating_grade=8, comment_text="Previous collection"
    )

    page = await client.get("/")
    csrf = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
    headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
    target = catalog_item(1)["url"]
    response = await client.post("/api/map/2", json={"url": target}, headers=headers)
    assert response.status_code == 200
    assert (await client.post("/api/jobs/item/2", json={}, headers=headers)).status_code == 200
    await engine.task

    row = next(row for row in engine.db.rows(engine.pid) if row["subject_id"] == 2)
    assert row["status"] == "migrated"
    assert row["resolution"]["basis"] == "manual"
    assert row["item"]["uuid"] == catalog_item(1)["uuid"]
    assert len(server.writes) == 1
    assert server.marks[catalog_item(1)["uuid"]]["shelf_type"] == "wishlist"
    assert server.marks[catalog_item(1)["uuid"]]["rating_grade"] == 6
    assert server.marks[catalog_item(1)["uuid"]]["comment_text"] == "Selected collection"


async def test_map_rejects_unparseable_url(web):
    import re

    client, app = web
    await client.get("/")
    engine = app.state.engine
    engine.credentials.put(
        "neodb",
        {
            "token": "NEO-SECRET",
            "instance": "https://neo.example",
            "user": {"url": "https://neo.example/users/user/"},
        },
    )
    engine.pid = "map-test-2"
    engine.db.profile(engine.pid, {})
    engine.db.import_snapshot(engine.pid, "scan", [collection(1)])
    engine.db.update(engine.pid, 1, status="resolve_failed", stage="resolve", error="需要手动匹配")
    page = await client.get("/")
    csrf = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
    headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
    response = await client.post("/api/map/1", json={"url": "javascript:alert(1)"}, headers=headers)
    assert response.status_code == 400
    row = engine.db.rows(engine.pid)[0]
    assert row["status"] == "resolve_failed"


@pytest.fixture
async def mapping_web(web, store):
    import re

    client, app = web
    engine = app.state.engine
    for platform in ("bangumi", "neodb"):
        engine.credentials.put(platform, store.get(platform))
    engine.select_profile()
    engine.db.import_snapshot(engine.pid, "scan", [collection(1), collection(2)])
    engine.db.update(engine.pid, 1, status="resolve_failed", error="等待选择")
    page = await client.get("/")
    csrf = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
    yield client, engine, {"Origin": ORIGIN, "X-CSRF-Token": csrf}


async def test_external_link_fetches_then_migrates_only_selected_entry(mapping_web, server):
    client, engine, headers = mapping_web
    source_url = "https://store.steampowered.com/app/123456/"
    original = server.handler
    times = []

    def handle(request):
        if request.url.path == "/api/catalog/fetch":
            assert request.url.host == "neo.example"
            assert request.url.params["url"] == source_url
            times.append(server.clock())
            if len(times) == 1:
                return httpx.Response(202)
            if len(times) == 2:
                return httpx.Response(429, headers={"Retry-After": "20"})
            return httpx.Response(302, headers={"Location": catalog_item(1)["api_url"]})
        return original(request)

    server.handler = handle
    response = await client.post("/api/map/1", json={"url": source_url}, headers=headers)
    assert response.status_code == 200
    assert response.json()["pending"] is True
    assert times == [0]
    response = await client.get("/api/map/1/pending")
    assert response.status_code == 200
    assert response.json()["pending"] is True
    assert response.json()["retry_after"] == 20
    assert times == [0, 0]
    response = await client.get("/api/map/1/pending")
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert times == [0, 0, 0]
    rows = engine.db.rows(engine.pid)
    assert rows[0]["item"]["uuid"] == catalog_item(1)["uuid"]
    assert rows[0]["resolution"]["basis"] == "manual"
    assert rows[1]["item"] is None
    assert not server.writes
    assert (
        await client.post("/api/jobs/item/1", json={"import_date": False}, headers=headers)
    ).status_code == 200
    await engine.task
    rows = engine.db.rows(engine.pid)
    assert [row["status"] for row in rows] == ["migrated", "pending"]
    assert len(server.writes) == 1
    import json

    assert "created_time" not in json.loads(server.writes[0].content)
    assert all(r.url.host in {"neo.example", "api.bgm.tv"} for r in server.requests)


@pytest.mark.parametrize("status", [202, 404, 422, 401])
async def test_external_link_failure_preserves_existing_mapping(mapping_web, server, status):
    client, engine, headers = mapping_web
    engine.db.update(
        engine.pid,
        1,
        item=catalog_item(2),
        resolution={"basis": "manual", "candidates": [catalog_item(2)]},
    )
    before = engine.db.rows(engine.pid)
    original = server.handler

    def handle(request):
        if request.url.path == "/api/catalog/fetch":
            return httpx.Response(status)
        return original(request)

    server.handler = handle
    response = await client.post(
        "/api/map/1", json={"url": "https://bgm.tv/subject/123"}, headers=headers
    )
    if status == 202:
        assert response.status_code == 200
        assert response.json()["pending"] is True
        after = engine.db.rows(engine.pid)
        assert after[0]["item"] == before[0]["item"]
        assert after[0]["resolution"]["pending_url"] == "https://bgm.tv/subject/123"
        assert server.clock() == 0
    else:
        assert response.status_code == 400
        assert engine.db.rows(engine.pid) == before
    assert not server.writes
