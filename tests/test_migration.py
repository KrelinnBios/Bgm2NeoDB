import pytest

from app.errors import AppError
from app.migrator import Migrator
from tests.conftest import catalog_item, collection, mark


async def run(engine, kind):
    if kind == "auto":
        engine.start(kind)
        await engine.task
        return
    engine.select_profile()
    source, target = engine.accounts()
    async with (
        engine.bangumi_factory(source["token"], cancel=engine.cancel) as bgm,
        engine.neodb_factory(target["instance"], target["token"], cancel=engine.cancel) as neo,
    ):
        try:
            if kind == "prepare":
                await engine.scan(bgm, source["user"])
                await engine.preview(neo)
            elif kind in {"preview", "retry"}:
                await engine.preview(neo, only_failures=kind == "retry")
            else:
                await engine.migrate(neo)
        except AppError as error:
            engine.job["message"] = str(error)


@pytest.fixture
async def engine(tmp_path, store, server):
    engine = Migrator(tmp_path, store, server.bgm, server.neo)
    yield engine
    await engine.close()


async def test_scan_never_writes_and_keeps_all_fields_in_database(engine, server):
    await run(engine, "prepare")
    assert engine.report()["ready"] == 1
    assert server.writes == []
    row = engine.db.rows(engine.pid)[0]
    assert row["source"] == server.sources[0]
    assert not list((engine.data / "profiles" / engine.pid).glob("*.json"))


async def test_explicit_preview_schedules_another_write_even_when_identical(engine, server):
    await run(engine, "prepare")
    await run(engine, "migrate")
    assert engine.report()["migrated"] == 1
    assert len(server.writes) == 1
    assert engine.report()["ratings_migrated"] == 1
    assert engine.report()["comments_migrated"] == 1
    await run(engine, "preview")
    assert engine.report()["ready"] == 1
    assert len(server.writes) == 1
    await run(engine, "migrate")
    assert len(server.writes) == 2
    assert engine.report()["migrated"] == 1
    assert engine.report()["ratings_migrated"] == 1
    assert engine.report()["comments_migrated"] == 1


async def test_merge_307_refetches_and_reads_target_before_writing(engine, server):
    import httpx

    await run(engine, "prepare")
    old_handler = server.handler
    merged = False

    def handler(request):
        nonlocal merged
        path = request.url.path
        if request.method == "POST" and path.endswith(catalog_item()["uuid"]):
            merged = True
            return httpx.Response(
                307, headers={"Location": f"/api/me/shelf/item/{catalog_item(2)['uuid']}"}
            )
        if merged and path == "/api/tv/" + catalog_item()["uuid"]:
            return httpx.Response(302, headers={"Location": catalog_item(2)["api_url"]})
        return old_handler(request)

    server.handler = handler
    await run(engine, "migrate")
    assert engine.report()["migrated"] == 1
    assert len(server.writes) == 1
    assert server.writes[0].url.path.endswith(catalog_item(2)["uuid"])
    assert engine.db.rows(engine.pid)[0]["item"]["uuid"] == catalog_item(2)["uuid"]


async def test_merge_to_existing_different_mark_does_not_overwrite(engine, server):
    import httpx

    await run(engine, "prepare")
    old_handler = server.handler
    server.marks[catalog_item(2)["uuid"]] = mark(2, comment_text="目标已有内容")

    def handler(request):
        if request.method == "POST" and request.url.path.endswith(catalog_item()["uuid"]):
            return httpx.Response(
                308, headers={"Location": f"/api/me/shelf/item/{catalog_item(2)['uuid']}"}
            )
        if request.url.path == "/api/tv/" + catalog_item()["uuid"]:
            return httpx.Response(302, headers={"Location": catalog_item(2)["api_url"]})
        return old_handler(request)

    server.handler = handler
    await run(engine, "migrate")
    assert engine.report()["conflict"] == 1
    assert not server.writes


async def test_interrupted_writing_row_requires_new_preview(engine, server):
    await run(engine, "prepare")
    engine.db.update(engine.pid, 1, status="writing", stage="write")
    assert engine.report()["needs_attention"] == 1
    await run(engine, "retry")
    assert engine.report()["ready"] == 1
    assert not server.writes


async def test_post_429_pauses_without_replaying(engine, server):
    import httpx

    await run(engine, "prepare")
    old_handler = server.handler
    times = []

    def handler(request):
        if request.method == "POST":
            times.append(server.clock())
            if len(times) == 1:
                return httpx.Response(429, headers={"Retry-After": "75"})
        return old_handler(request)

    server.handler = handler
    await run(engine, "migrate")
    assert engine.report()["migrated"] == 0
    assert len(times) == 1
    assert "已暂停" in engine.job["message"]


async def test_existing_target_fields_privacy_and_date_survive(engine, server):
    uid = catalog_item()["uuid"]
    server.sources[0].update(type=4, comment="", tags=[])
    server.marks[uid] = mark(rating_grade=9, comment_text="原短评", tags=["原标签"], visibility=2)
    await run(engine, "prepare")
    await run(engine, "migrate")
    target = server.marks[uid]
    assert target["shelf_type"] == "progress"
    assert target["rating_grade"] == 9
    assert target["comment_text"] == "原短评"
    assert target["tags"] == ["原标签"]
    assert target["visibility"] == 2
    assert target["created_time"] == "2021-01-01T00:00:00Z"
    assert not target["post_to_fediverse"]


@pytest.mark.parametrize("mode", ["migrate", "auto"])
async def test_tag_merge_reuses_existing_spelling_and_verifies(engine, server, mode):
    uid = catalog_item()["uuid"]
    server.sources[0]["tags"] = ["WEB", " web ", "动画", "新标签"]
    server.marks[uid] = mark(tags=["Web", "动画"])
    await run(engine, "prepare")
    await run(engine, mode)
    assert server.marks[uid]["tags"] == ["Web", "动画", "新标签"]
    assert engine.report()["migrated"] == 1


async def test_tag_on_another_item_is_reused(engine, server):
    other_uid = catalog_item(2)["uuid"]
    server.marks[other_uid] = mark(2, tags=["Web", "Android"])
    original = dict(server.marks[other_uid])
    server.sources[0]["tags"] = ["WEB", "ANDROID"]
    await run(engine, "auto")
    assert server.marks[catalog_item()["uuid"]]["tags"] == ["Web", "Android"]
    assert server.marks[other_uid] == original
    assert engine.report()["migrated"] == 1


async def test_concurrent_items_share_new_tag_spelling(engine, server):
    server.sources = [collection(1, tags=["Web"]), collection(2, tags=["WEB"])]
    await run(engine, "auto")
    tags = [server.marks[catalog_item(sid)["uuid"]]["tags"] for sid in (1, 2)]
    assert tags[0] == tags[1] == ["Web"]
    assert engine.report()["migrated"] == 2
    assert sum(r.url.path == "/api/me/tag/" for r in server.requests) == 1


async def test_saved_plan_preserves_new_tag_spelling_after_restart(engine, server):
    server.sources = [collection(1, tags=["Web"]), collection(2, tags=["WEB"])]
    await run(engine, "prepare")
    row = engine.db.rows(engine.pid, subject_id=2)[0]
    async with server.neo("https://neo.example") as neo:
        await engine.migrate(neo, rows=[row])
    assert server.marks[catalog_item(2)["uuid"]]["tags"] == ["Web"]
    assert engine.report()["migrated"] == 1


async def test_case_duplicates_in_readback_are_not_success(engine, server):
    server.sources[0]["tags"] = ["Web"]
    await run(engine, "prepare")
    server.after_write = lambda target: target.update(tags=["Web", "WEB"])
    await run(engine, "migrate")
    assert engine.report()["partial"] == 1
    assert engine.report()["migrated"] == 0


async def test_tag_catalog_change_requires_new_preview_before_write(engine, server):
    server.sources[0]["tags"] = ["Web"]
    await run(engine, "prepare")
    server.tags = ["WEB"]
    await run(engine, "migrate")
    assert not server.writes
    assert engine.report()["conflict"] == 1
    await run(engine, "auto")
    assert server.marks[catalog_item()["uuid"]]["tags"] == ["WEB"]
    assert engine.report()["migrated"] == 1


async def test_incomplete_tag_catalog_stops_before_writing(engine, server):
    import httpx

    original = server.handler

    def handler(request):
        if request.url.path == "/api/me/tag/":
            return httpx.Response(200, json={"data": [], "pages": 2, "count": 101})
        return original(request)

    server.handler = handler
    await run(engine, "auto")
    assert not server.writes
    assert engine.report()["migrated"] == 0
    assert "标签" in engine.job["message"]


async def test_preview_staleness_blocks_overwrite(engine, server):
    await run(engine, "prepare")
    server.marks[catalog_item()["uuid"]] = mark(comment_text="刚修改")
    await run(engine, "migrate")
    assert not server.writes
    assert engine.report()["conflict"] == 1


async def test_uncertain_post_re_reads_before_retry(engine, server):
    await run(engine, "prepare")
    server.post_commits_then_disconnect = True
    await run(engine, "migrate")
    assert len(server.writes) == 1
    assert engine.report()["partial"] == 1
    await run(engine, "auto")
    assert len(server.writes) == 2
    assert engine.report()["migrated"] == 1


@pytest.mark.parametrize("mode", ["migrate", "auto"])
async def test_identical_marks_are_written_and_verified(engine, server, mode):
    from app.models import plan_collection

    uid = catalog_item()["uuid"]
    server.marks[uid] = mark(**plan_collection(server.sources[0], None)["payload"])
    await run(engine, "prepare")
    row = engine.db.rows(engine.pid)[0]
    assert row["plan"]["diff"] == {}
    assert row["status"] == "ready"
    assert not server.writes
    await run(engine, mode)
    assert len(server.writes) == 1
    assert engine.report()["migrated"] == 1
    assert engine.report()["skipped"] == 0
    assert engine.report()["ratings_migrated"] == 1
    assert engine.report()["comments_migrated"] == 1
    assert engine.report()["tags_migrated"] == 1
    assert server.requests[-1].method == "GET"
    assert server.requests[-1].url.path.endswith(uid)


@pytest.mark.parametrize("mode", ["migrate", "auto"])
async def test_legacy_skipped_entries_are_written(engine, server, mode):
    from app.models import plan_collection

    server.marks[catalog_item()["uuid"]] = mark(
        **plan_collection(server.sources[0], None)["payload"]
    )
    await run(engine, "prepare")
    plan = engine.db.rows(engine.pid)[0]["plan"]
    plan["action"] = "skip"
    engine.db.update(engine.pid, 1, status="skipped", plan=plan)
    await run(engine, mode)
    assert len(server.writes) == 1
    assert engine.report()["migrated"] == 1
    assert engine.report()["skipped"] == 0
    # Continuing an automatic job does not repeatedly rewrite verified successes.
    await run(engine, "auto")
    assert len(server.writes) == 1


@pytest.mark.parametrize("failure", [503, 422])
async def test_identical_mark_with_rejected_post_is_not_success(engine, server, failure):
    from app.models import plan_collection

    server.marks[catalog_item()["uuid"]] = mark(
        **plan_collection(server.sources[0], None)["payload"]
    )
    await run(engine, "prepare")
    server.fail_write = failure
    await run(engine, "migrate")
    assert len(server.writes) == 1
    assert engine.report()["migrated"] == 0
    assert engine.report()["partial"] == 1


async def test_mark_becomes_identical_after_preview_still_posts(engine, server):
    await run(engine, "prepare")
    plan = engine.db.rows(engine.pid)[0]["plan"]
    server.marks[catalog_item()["uuid"]] = mark(**plan["payload"])
    await run(engine, "migrate")
    assert len(server.writes) == 1
    assert engine.report()["migrated"] == 1


async def test_failed_verification_not_marked_success(engine, server):
    await run(engine, "prepare")
    server.after_write = lambda target: target.update(rating_grade=1)
    await run(engine, "migrate")
    assert engine.report()["partial"] == 1
    assert engine.report()["migrated"] == 0


async def test_write_transient_retries_bounded(engine, server):
    await run(engine, "prepare")
    server.fail_write = 503
    await run(engine, "migrate")
    assert len(server.writes) == 1
    assert engine.report()["partial"] == 1


async def test_auth_failure_stops_batch(engine, server):
    server.sources = [collection(1), collection(2)]
    await run(engine, "prepare")
    server.fail_write = 401
    await run(engine, "migrate")
    assert len(server.writes) == 1
    assert "登录已失效" in engine.job["message"]


async def test_resume_uses_saved_preview_after_restart(tmp_path, store, server):
    first = Migrator(tmp_path, store, server.bgm, server.neo)
    await run(first, "prepare")
    await first.close()
    second = Migrator(tmp_path, store, server.bgm, server.neo)
    await run(second, "migrate")
    assert second.report()["migrated"] == 1
    await second.close()


async def test_failed_retry_only_previews_then_user_starts(engine, server):
    await run(engine, "prepare")
    server.fail_write = 422
    await run(engine, "migrate")
    server.fail_write = None
    before = len(server.writes)
    await run(engine, "retry")
    assert len(server.writes) == before
    assert engine.report()["ready"] == 1
    await run(engine, "migrate")
    assert engine.report()["migrated"] == 1


async def test_account_isolation(engine, server, store):
    await run(engine, "prepare")
    previous = engine.pid
    value = store.get("bangumi").copy()
    value["user"] = {"id": 200, "username": "other"}
    store.put("bangumi", value)
    engine.select_profile()
    assert engine.pid != previous
    assert not engine.db.rows(engine.pid)
    with pytest.raises(AppError):
        engine.start("migrate")


async def test_duplicate_target_mapping_is_blocked(engine, server):
    server.sources = [collection(1), collection(2)]
    server.collision = True
    await run(engine, "prepare")
    assert engine.report()["conflict"] == 2
    assert engine.report()["ready"] == 0
    assert not server.writes


async def test_private_unknown_blocked_before_resolve(engine, server):
    del server.sources[0]["private"]
    await run(engine, "prepare")
    assert engine.report()["blocked_private_visibility"] == 1
    assert not any(r.url.path == "/api/catalog/fetch" for r in server.requests)


async def test_secrets_never_written_to_data_files(engine, server):
    await run(engine, "prepare")
    await run(engine, "migrate")
    for path in engine.data.rglob("*"):
        if path.is_file():
            content = path.read_bytes()
            assert b"BGM-SECRET" not in content
            assert b"NEO-SECRET" not in content


async def test_rescan_excludes_removed_sources_without_backup_files(engine, server):
    server.sources = [collection(1), collection(2)]
    await run(engine, "prepare")
    server.sources = [collection(2)]
    await run(engine, "prepare")
    assert [r["subject_id"] for r in engine.db.rows(engine.pid)] == [2]
    assert not (engine.data / "profiles" / engine.pid / "snapshots").exists()


async def test_scan_automatically_writes_without_manual_step(engine, server):
    engine.start("scan")
    await engine.task
    assert len(server.writes) == 1
    assert engine.report()["migrated"] == 1
    assert engine.report()["ready"] == 0


async def test_quick_matches_write_before_deferred_resolution(engine, server):
    import httpx

    server.sources = [collection(1), collection(2)]
    original = server.handler
    events = []
    slow_calls = 0

    def handle(request):
        nonlocal slow_calls
        if request.url.path == "/api/catalog/fetch":
            sid = int(request.url.params["url"].rsplit("/", 1)[-1])
            events.append(("resolve", sid))
            if sid == 1:
                slow_calls += 1
                if slow_calls == 1:
                    return httpx.Response(202)
        if request.method == "POST":
            events.append(("write", int(request.url.path.rsplit("-", 1)[-1])))
        return original(request)

    server.handler = handle
    await run(engine, "auto")
    assert events.index(("write", 2)) < events.index(("resolve", 1), 1)
    assert [sid for event, sid in events if event == "write"] == [2, 1]
    assert engine.report()["migrated"] == 2


async def test_final_resolution_failure_is_bounded_and_reported(engine, server):
    import httpx

    server.sources = [collection(1), collection(2)]
    original = server.handler
    calls = 0

    def handle(request):
        nonlocal calls
        if request.url.path == "/api/catalog/fetch" and request.url.params["url"].endswith("/1"):
            calls += 1
            return httpx.Response(404)
        return original(request)

    server.handler = handle
    await run(engine, "auto")
    assert calls == 1
    assert engine.report()["migrated"] == 1
    assert engine.report()["resolve_failed"] == 1
    assert engine.report()["pending"] == 0
    assert "1 个条目需要处理" in engine.job["message"]


@pytest.mark.parametrize("verification_failure", [False, True])
async def test_auto_pauses_on_write_or_verification_failure(engine, server, verification_failure):
    server.sources = [collection(sid) for sid in range(1, 71)]
    if verification_failure:
        server.after_write = lambda target: target.update(rating_grade=1)
    else:
        server.fail_write = 503
    await run(engine, "auto")
    assert len(server.writes) == 1
    assert engine.report()["migrated"] == 0
    assert engine.report()["partial"] == 1
    assert engine.report()["pending"] + engine.report()["ready"] == 69
    assert "已暂停" in engine.job["message"]
    assert not engine.job["running"]
    server.after_write = None
    server.fail_write = None
    await run(engine, "auto")
    assert engine.report()["migrated"] == 70
    assert engine.report()["needs_attention"] == 0


async def test_inflight_success_finishes_verification_when_another_write_fails(engine, server):
    import asyncio

    import httpx

    server.sources = [collection(1), collection(2), collection(3)]
    original = server.handler
    release = asyncio.Event()

    async def handle(request):
        if request.method == "POST":
            sid = int(request.url.path.rsplit("-", 1)[-1])
            if sid == 1:
                await release.wait()
            elif sid == 2:
                release.set()
                return httpx.Response(503)
        return original(request)

    server.handler = handle
    await asyncio.wait_for(run(engine, "auto"), timeout=3)
    rows = {row["subject_id"]: row for row in engine.db.rows(engine.pid)}
    assert rows[1]["status"] == "migrated"
    assert rows[2]["status"] == "partial"
    assert rows[3]["status"] in {"pending", "ready"}
    assert "已暂停" in engine.job["message"]


async def test_auth_error_during_quick_resolution_stops_without_loop(engine, server):
    import asyncio

    import httpx

    original = server.handler

    def handle(request):
        if request.url.path == "/api/catalog/fetch":
            return httpx.Response(401)
        return original(request)

    server.handler = handle
    await asyncio.wait_for(run(engine, "auto"), timeout=3)
    assert not server.writes
    assert "登录已失效" in engine.job["message"]


async def test_deferred_item_gets_more_than_thirty_seconds_then_writes(engine, server):
    import httpx

    server.sources = [collection(1), collection(2)]
    original = server.handler
    writes = []

    def handle(request):
        if (
            request.url.path == "/api/catalog/fetch"
            and request.url.params["url"].endswith("/1")
            and server.clock() < 60
        ):
            return httpx.Response(202)
        if request.method == "POST":
            writes.append((int(request.url.path.rsplit("-", 1)[-1]), server.clock()))
        return original(request)

    server.handler = handle
    await run(engine, "auto")
    assert [sid for sid, _ in writes] == [2, 1]
    assert writes[0][1] < 30
    assert 60 <= writes[1][1] < 120
    assert engine.report()["migrated"] == 2
    assert engine.report()["resolve_failed"] == 0


async def test_stuck_fetch_falls_back_to_exact_title_search(engine, server):
    import httpx

    server.sources = [collection(1)]
    original = server.handler

    def handle(request):
        if request.url.path == "/api/catalog/fetch":
            return httpx.Response(202)
        return original(request)

    server.handler = handle
    server.search_hits = [catalog_item(1)]
    await run(engine, "auto")
    assert engine.report()["migrated"] == 1
    assert engine.report()["resolve_failed"] == 0
    assert any(r.url.path == "/api/catalog/search" for r in server.requests)


async def test_final_fetch_budget_is_shared_with_queued_rows(engine, server, monkeypatch):
    import httpx

    from app.neodb import NeoDB

    server.sources = [collection(sid) for sid in range(1, 14)]
    original_handler = server.handler
    original_resolve = NeoDB.resolve
    started = []

    async def resolve(api, sid, *, wait=True, timeout=30):
        if wait and not started:
            started.append(server.clock())
        return await original_resolve(api, sid, wait=wait, timeout=timeout)

    def handle(request):
        if request.url.path == "/api/catalog/fetch":
            return httpx.Response(202)
        return original_handler(request)

    monkeypatch.setattr(NeoDB, "resolve", resolve)
    server.handler = handle
    await run(engine, "auto")
    assert server.clock() - started[0] <= 60.01
    assert engine.report()["resolve_failed"] == 13
    assert engine.report()["pending"] == 0
    assert not server.writes
    assert all("共用的 60 秒" in row["error"] for row in engine.db.rows(engine.pid))


async def test_final_budget_also_bounds_stalled_search(engine, server, monkeypatch):
    import asyncio
    import time

    monkeypatch.setattr("app.migrator.RESOLVE_FINAL_TIMEOUT", 0.05)
    engine.select_profile()
    engine.db.import_snapshot(engine.pid, "budget", [collection(sid) for sid in range(1, 9)])
    searches = 0

    async def lookup(neo, source, exclude=()):
        nonlocal searches
        searches += 1
        if searches > 8:
            await asyncio.sleep(5)
        return None

    async def resolve(*args, **kwargs):
        return None

    monkeypatch.setattr(engine, "lookup_by_title", lookup)
    async with server.neo("https://neo.example", "token") as neo:
        neo.clock = time.monotonic
        monkeypatch.setattr(neo, "resolve", resolve)
        await asyncio.wait_for(engine.preview_and_migrate(neo), timeout=3)
    assert engine.report()["resolve_failed"] == 8
    assert engine.report()["pending"] == 0
    assert not server.writes


async def test_final_budget_does_not_cancel_started_write(engine, server, monkeypatch):
    import asyncio
    import time

    monkeypatch.setattr("app.migrator.RESOLVE_FINAL_TIMEOUT", 1.0)
    engine.select_profile()
    engine.db.import_snapshot(engine.pid, "budget", [collection(1), collection(2)])
    searches = {}
    write_started = asyncio.Event()

    async def lookup(neo, source, exclude=()):
        sid = source["subject_id"]
        searches[sid] = searches.get(sid, 0) + 1
        if searches[sid] == 1:
            return None
        if sid == 2:
            await asyncio.sleep(5)
        return neo.parse_item(catalog_item(sid))

    async def resolve(*args, **kwargs):
        return None

    monkeypatch.setattr(engine, "lookup_by_title", lookup)
    async with server.neo("https://neo.example", "token") as neo:
        neo.clock = time.monotonic
        original_write = neo.write_shelf

        async def slow_write(item, payload):
            write_started.set()
            await asyncio.sleep(1.1)
            return await original_write(item, payload)

        monkeypatch.setattr(neo, "resolve", resolve)
        monkeypatch.setattr(neo, "write_shelf", slow_write)
        task = asyncio.create_task(engine.preview_and_migrate(neo))
        try:
            await asyncio.wait_for(write_started.wait(), timeout=1)
            await asyncio.wait_for(task, timeout=3)
        finally:
            if not task.done():
                task.cancel()
                await task
    rows = {row["subject_id"]: row for row in engine.db.rows(engine.pid)}
    assert rows[1]["status"] == "migrated"
    assert rows[2]["status"] == "resolve_failed"
    assert len(server.writes) == 1


async def test_existing_exact_match_does_not_wait_for_catalog_fetch(engine, server):
    server.sources = [collection(1)]
    server.search_hits = [catalog_item(1)]
    await run(engine, "auto")
    assert engine.report()["migrated"] == 1
    assert not any(r.url.path == "/api/catalog/fetch" for r in server.requests)


async def test_collision_skips_only_that_row_and_batch_continues(engine, server):
    """撞车的行排在最前时，不能让后面的条目全部不被处理。"""
    server.sources = [collection(sid) for sid in range(1, 6)]
    server.collision = True
    server.search_hits = []
    await run(engine, "auto")
    statuses = {row["subject_id"]: row["status"] for row in engine.db.rows(engine.pid)}
    # 一条拿到该作品并写入，其余判为冲突，但都必须被处理过（不留 pending）
    assert sum(s == "migrated" for s in statuses.values()) == 1
    assert sum(s == "conflict" for s in statuses.values()) == 4
    assert not any(s == "pending" for s in statuses.values())
    assert len(server.writes) == 1


async def test_series_seasons_are_separated_by_title_search(engine, server):
    """catalog/fetch 把各季都解析到第一季时，用标题搜索错开。"""
    import httpx

    seasons = {1: "少年骇客 第一季", 2: "少年骇客 第二季"}
    server.sources = [
        collection(sid, subject={"id": sid, "name": name, "name_cn": name})
        for sid, name in seasons.items()
    ]
    hits = []
    for sid, name in seasons.items():
        hit = catalog_item(sid)
        hit["title"] = name
        hits.append(hit)
    server.search_hits = hits
    original = server.handler

    def handle(request):
        # 两个条目的 bgm 地址都被解析到第一季
        if request.url.path == "/api/catalog/fetch":
            item = catalog_item(1)
            return httpx.Response(
                302, headers={"Location": item["api_url"]}, json={"url": item["api_url"]}
            )
        return original(request)

    server.handler = handle
    await run(engine, "auto")
    rows = {row["subject_id"]: row for row in engine.db.rows(engine.pid)}
    assert rows[1]["item"]["uuid"] == catalog_item(1)["uuid"]
    assert rows[2]["item"]["uuid"] == catalog_item(2)["uuid"]
    assert engine.report()["migrated"] == 2
    assert engine.report()["conflict"] == 0


async def test_fetch_404_matches_localized_title_without_waiting(engine, server):
    import httpx

    server.sources = [
        collection(
            1,
            subject={"id": 1, "name": "沙耶の唄", "name_cn": "沙耶之歌", "type": 4},
            subject_type=4,
        )
    ]
    hit = catalog_item(1)
    hit["title"] = "The Song of Saya"
    hit["type"] = "Game"
    hit["localized_title"] = [{"lang": "zh-cn", "text": "沙耶之歌"}]
    server.search_hits = [hit]
    original = server.handler

    def handle(request):
        if request.url.path == "/api/catalog/fetch":
            return httpx.Response(404)
        return original(request)

    server.handler = handle
    await run(engine, "auto")
    assert engine.report()["migrated"] == 1
    assert engine.report()["resolve_failed"] == 0
    assert engine.report()["pending"] == 0
    assert server.clock() < 30


async def test_no_search_match_reports_resolve_failed_with_manual_hint(engine, server):
    import httpx

    server.sources = [collection(7)]
    original = server.handler

    def handle(request):
        if request.url.path == "/api/catalog/fetch":
            return httpx.Response(202)
        return original(request)

    server.handler = handle
    await run(engine, "auto")
    assert engine.report()["resolve_failed"] == 1
    assert "手动选择对应条目" in engine.db.rows(engine.pid)[0]["error"]


async def test_auto_reaches_eight_writers_and_stops_queued_writes_on_failure(engine, server):
    import asyncio

    import httpx

    from app.migrator import WRITE_CONCURRENCY

    server.sources = [collection(sid) for sid in range(1, 25)]
    engine.select_profile()
    engine.db.import_snapshot(engine.pid, "concurrency", server.sources)
    for source in server.sources:
        engine.db.update(engine.pid, source["subject_id"], item=catalog_item(source["subject_id"]))
    original = server.handler
    release = asyncio.Event()
    started, active, peak = [], 0, 0

    async def handle(request):
        nonlocal active, peak
        if request.method != "POST":
            return original(request)
        started.append(request)
        active += 1
        peak = max(peak, active)
        try:
            if len(started) == WRITE_CONCURRENCY:
                release.set()
                return httpx.Response(503)
            await release.wait()
            return original(request)
        finally:
            active -= 1

    server.handler = handle
    await asyncio.wait_for(run(engine, "auto"), timeout=5)
    assert peak == WRITE_CONCURRENCY == 10
    assert len(started) == 10
    report = engine.report()
    assert report["migrated"] == 9
    assert report["partial"] == 1
    assert report["ready"] + report["pending"] == 14
    assert "已暂停" in engine.job["message"]


async def test_concurrent_targets_merged_before_write_are_not_overwritten(engine, server):
    import asyncio

    import httpx

    server.sources = [collection(1), collection(2)]
    server.marks[catalog_item(1)["uuid"]] = mark(1)
    original = server.handler
    reads = {}

    async def handle(request):
        if request.method == "GET" and "/shelf/item/" in request.url.path:
            sid = int(request.url.path.rsplit("-", 1)[-1])
            reads[sid] = reads.get(sid, 0) + 1
            if sid == 2 and reads[sid] == 2:
                return httpx.Response(
                    302,
                    headers={
                        "Location": "/api/me/shelf/item/" + catalog_item(1)["uuid"],
                    },
                )
        if request.method == "POST":
            await asyncio.sleep(0.02)
        return original(request)

    server.handler = handle
    await run(engine, "auto")
    assert len(server.writes) == 1
    assert engine.report()["migrated"] == 1
    assert engine.report()["conflict"] == 1


async def test_batch_full_table_reads_do_not_grow_per_item(engine, server, monkeypatch):
    server.sources = [collection(sid) for sid in range(1, 25)]
    original = engine.db.rows
    full_reads = 0

    def rows(pid, *, subject_id=None):
        nonlocal full_reads
        if subject_id is None:
            full_reads += 1
        return original(pid, subject_id=subject_id)

    monkeypatch.setattr(engine.db, "rows", rows)
    await run(engine, "auto")
    assert len(server.writes) == 24
    assert full_reads <= 6


async def test_single_item_conflict_does_not_claim_success(engine, server):
    engine.select_profile()
    engine.db.import_snapshot(engine.pid, "single", [collection(1)])
    engine.db.update(
        engine.pid,
        1,
        status="conflict",
        resolution={
            "code": "ambiguous",
            "retryable": False,
        },
    )
    engine.start_item(1)
    await engine.task
    assert not server.writes
    assert "尚未迁移成功" in engine.job["message"]
