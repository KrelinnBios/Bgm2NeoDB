import asyncio

import httpx
import pytest

from app.auth import OAuth
from app.bangumi import Bangumi
from app.config import USER_AGENT
from app.errors import AppError, AuthError, Cancelled, DeadlineExceeded, RequestFailed
from app.http import APIClient
from app.neodb import NeoDB
from tests.conftest import Clock, catalog_item, collection


@pytest.mark.parametrize(
    "url",
    [
        "https://bgm.tv/subject/123",
        "https://store.steampowered.com/app/123456/?l=schinese",
        "http://bangumi.tv/subject/123",
        catalog_item()["url"],
        "https://neo.example/tv/season/" + catalog_item()["uuid"],
    ],
)
async def test_manual_link_resolves_via_connected_instance_only(url):
    calls = []

    def handle(request):
        calls.append(request)
        assert request.url.host == "neo.example"
        assert request.headers["Authorization"] == "Bearer NEO-SECRET"
        if request.url.path == "/api/catalog/fetch":
            assert request.url.params["url"] == url
            return httpx.Response(302, json={"url": catalog_item()["api_url"]})
        return httpx.Response(200, json=catalog_item())

    async with NeoDB(
        "https://neo.example", "NEO-SECRET", transport=httpx.MockTransport(handle), interval=0
    ) as api:
        item = await api.resolve_item_url("  " + url + "  ")
    assert item["uuid"] == catalog_item()["uuid"]
    assert len(calls) == 2


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "file:///etc/passwd",
        "//bgm.tv/subject/123",
        "/subject/123",
        "https:///subject/123",
        "https://user:password@bgm.tv/subject/123",
        "https://bgm.tv:bad/subject/123",
        "https://[invalid/subject/123",
        "https://bgm.tv/subject/1\n23",
        "https://bgm.tv\\@evil.example/subject/123",
    ],
)
async def test_manual_link_rejects_invalid_input_without_requests(url):
    calls = []
    async with NeoDB(
        "https://neo.example", transport=httpx.MockTransport(lambda r: calls.append(r))
    ) as api:
        with pytest.raises(AppError, match="完整的"):
            await api.resolve_item_url(url)
    assert not calls


async def test_manual_link_does_not_follow_external_fetch_redirect():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://bgm.tv/subject/123"})

    async with NeoDB(
        "https://neo.example", "NEO-SECRET", transport=httpx.MockTransport(handle)
    ) as api:
        with pytest.raises(AppError, match="其他实例"):
            await api.resolve_item_url("https://bgm.tv/subject/123")
    assert len(calls) == 1
    assert calls[0].url.host == "neo.example"


async def test_manual_link_pending_wait_can_be_cancelled():
    clock = Clock()
    cancel = asyncio.Event()

    async def sleep(seconds):
        await clock.sleep(seconds)
        cancel.set()

    async with NeoDB(
        "https://neo.example",
        transport=httpx.MockTransport(lambda r: httpx.Response(202)),
        clock=clock,
        sleep=sleep,
        cancel=cancel,
    ) as api:
        with pytest.raises(Cancelled):
            await api.resolve_item_url("https://bgm.tv/subject/123")
    assert clock() == 0.5


async def test_manual_link_deadline_also_bounds_in_flight_request():
    async def handle(request):
        await asyncio.Event().wait()

    async with NeoDB("https://neo.example", transport=httpx.MockTransport(handle)) as api:
        with pytest.raises(AppError, match="尚未完成抓取"):
            await api.resolve_item_url("https://bgm.tv/subject/123", timeout=0.02)


async def test_fetch_202_waits_at_least_15_then_302():
    clock = Clock()
    times = []

    def handle(request):
        if request.url.path == "/api/catalog/fetch":
            times.append(clock())
            if len(times) == 1:
                return httpx.Response(202, json={"message": "pending"})
            return httpx.Response(302, json={"url": catalog_item()["api_url"]})
        return httpx.Response(200, json=catalog_item())

    async with NeoDB(
        "https://neo.example",
        "secret",
        transport=httpx.MockTransport(handle),
        clock=clock,
        sleep=clock.sleep,
        interval=0,
    ) as api:
        assert (await api.resolve(1))["uuid"] == catalog_item()["uuid"]
    assert times[1] - times[0] >= 15


@pytest.mark.parametrize("timeout", [30, 120])
async def test_fetch_stops_at_deadline(timeout):
    clock = Clock()
    async with NeoDB(
        "https://neo.example",
        transport=httpx.MockTransport(lambda r: httpx.Response(202)),
        clock=clock,
        sleep=clock.sleep,
        interval=0,
    ) as api:
        with pytest.raises(DeadlineExceeded, match=str(timeout)):
            await api.resolve(1, timeout=timeout)
    assert clock() == timeout


@pytest.mark.parametrize("status", [202, 429])
async def test_quick_resolve_defers_without_polling(status):
    clock = Clock()
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status)

    async with NeoDB(
        "https://neo.example",
        transport=httpx.MockTransport(handle),
        clock=clock,
        sleep=clock.sleep,
        interval=0,
    ) as api:
        assert await api.resolve(1, wait=False) is None
    assert len(calls) == 1
    assert clock() == 0


async def test_quick_manual_link_defers_without_polling():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(202)

    async with NeoDB(
        "https://neo.example",
        transport=httpx.MockTransport(handle),
        interval=0,
    ) as api:
        assert await api.resolve_item_url("https://bgm.tv/subject/123", wait=False) is None
    assert len(calls) == 1


async def test_fetch_cooldown_survives_quick_pass_without_delaying_other_sources():
    clock = Clock()
    calls = []

    def handle(request):
        if request.url.path == "/api/catalog/fetch":
            sid = int(request.url.params["url"].rsplit("/", 1)[-1])
            calls.append((sid, clock()))
            if len(calls) == 1:
                return httpx.Response(202)
            return httpx.Response(200, json=catalog_item(sid))
        raise AssertionError(request.url.path)

    async with NeoDB(
        "https://neo.example",
        transport=httpx.MockTransport(handle),
        clock=clock,
        sleep=clock.sleep,
        interval=0,
    ) as api:
        assert await api.resolve(1, wait=False) is None
        assert (await api.resolve(2, wait=False))["uuid"] == catalog_item(2)["uuid"]
        assert (await api.resolve(1))["uuid"] == catalog_item(1)["uuid"]
    assert calls == [(1, 0), (2, 3), (1, 15)]


async def test_extended_resolution_can_be_paused():
    clock = Clock()
    cancel = asyncio.Event()

    async def sleep(seconds):
        await clock.sleep(seconds)
        if clock() >= 35:
            cancel.set()

    async with NeoDB(
        "https://neo.example",
        transport=httpx.MockTransport(lambda request: httpx.Response(202)),
        clock=clock,
        sleep=sleep,
        cancel=cancel,
        interval=0,
    ) as api:
        with pytest.raises(Cancelled):
            await api.resolve(1, timeout=120)
    assert 35 <= clock() < 36


async def test_search_returns_parseable_items_and_exact_match():
    clock = Clock()
    first = catalog_item()
    other = catalog_item(2)
    first["title"] = "沙耶之歌"
    other["title"] = "沙耶の唄 / 沙耶之歌"
    hits = [first, other]

    def handle(request):
        if request.url.path == "/api/catalog/search":
            return httpx.Response(200, json={"data": hits})
        raise AssertionError(request.url.path)

    async with NeoDB(
        "https://neo.example",
        transport=httpx.MockTransport(handle),
        clock=clock,
        sleep=clock.sleep,
        interval=0,
    ) as api:
        items = await api.search("沙耶之歌")
        assert [it["uuid"] for it in items] == [hits[0]["uuid"], hits[1]["uuid"]]
        match = api.exact_match(items, ["沙耶之歌"], {"tvseason"})
        assert match["uuid"] == first["uuid"]
        assert api.exact_match(items, ["沙耶之歌"], {"game"}) is None
        assert api.exact_match(items, ["不存在的标题"], {"tvseason"}) is None


async def test_search_exact_tries_each_title_and_prefers_type():
    import copy

    clock = Clock()
    game = catalog_item(1)
    game["title"] = "少女猎杀"
    game["type"] = "Game"
    movie = catalog_item(2)
    movie["title"] = "少女猎杀"
    movie["type"] = "Movie"

    def handle(request):
        assert request.url.path == "/api/catalog/search"
        return httpx.Response(200, json={"data": [copy.deepcopy(game), copy.deepcopy(movie)]})

    async with NeoDB(
        "https://neo.example",
        "secret",
        transport=httpx.MockTransport(handle),
        clock=clock,
        sleep=clock.sleep,
        interval=0,
    ) as api:
        match = await api.search_exact(["少女猎杀"], {"game"})
        assert match["uuid"] == game["uuid"]
        assert match["type"] == "Game"


async def test_exact_match_uses_localized_title_and_slash_alias():
    clock = Clock()
    game = catalog_item(1)
    game["title"] = "The Song of Saya"
    game["type"] = "Game"
    game["localized_title"] = [{"lang": "zh-cn", "text": "沙耶之歌"}]
    movie = catalog_item(2)
    movie["title"] = "沙耶之歌"
    movie["type"] = "Movie"
    alias = catalog_item(3)
    alias["title"] = "沙耶の唄 / 沙耶之歌"
    alias["type"] = "Game"

    def handle(request):
        if request.url.path == "/api/catalog/search":
            return httpx.Response(200, json={"data": [game, movie, alias]})
        raise AssertionError(request.url.path)

    async with NeoDB(
        "https://neo.example",
        transport=httpx.MockTransport(handle),
        clock=clock,
        sleep=clock.sleep,
        interval=0,
    ) as api:
        items = await api.search("沙耶之歌")
        match = api.exact_match(items, ["沙耶之歌", "沙耶の唄"], {"game"})
        assert match["uuid"] == game["uuid"]
        slash = api.exact_match([api.parse_item(alias)], ["沙耶之歌"], {"game"})
        assert slash["uuid"] == alias["uuid"]


async def test_item_from_page_url_fetches_api_json():
    clock = Clock()
    item = catalog_item(1)

    def handle(request):
        assert request.url.path == "/api/game/" + item["uuid"]
        return httpx.Response(200, json=item)

    async with NeoDB(
        "https://neo.example",
        transport=httpx.MockTransport(handle),
        clock=clock,
        sleep=clock.sleep,
        interval=0,
    ) as api:
        result = await api.item_from_url(f"https://neo.example/game/{item['uuid']}")
        assert result["uuid"] == item["uuid"]


async def test_slow_requests_overlap_but_starts_remain_spaced():
    clock = Clock()
    started = []
    all_started = asyncio.Event()
    release = asyncio.Event()

    async def handle(request):
        started.append(clock())
        if len(started) == 4:
            all_started.set()
        await release.wait()
        return httpx.Response(200)

    async with APIClient(
        "https://neo.example",
        transport=httpx.MockTransport(handle),
        clock=clock,
        sleep=clock.sleep,
        interval=0.25,
    ) as api:
        tasks = [asyncio.create_task(api.request("GET", "/test")) for _ in range(4)]
        try:
            # All requests must start before any response is released.
            await asyncio.wait_for(all_started.wait(), timeout=2)
            assert started == [0, 0.25, 0.5, 0.75]
        finally:
            release.set()
            await asyncio.gather(*tasks)


@pytest.mark.parametrize("cancelled", [False, True])
async def test_waiting_request_rechecks_deadline_and_cancellation(cancelled):
    clock = Clock()
    calls = []
    async with APIClient(
        "https://neo.example",
        transport=httpx.MockTransport(lambda request: calls.append(request)),
        clock=clock,
        sleep=clock.sleep,
        interval=0,
    ) as api:
        async with api._slot:
            task = asyncio.create_task(api.request("GET", "/test", deadline=1))
            await asyncio.sleep(0)
            if cancelled:
                api.cancel.set()
            else:
                clock.value = 2
        with pytest.raises(Cancelled if cancelled else DeadlineExceeded):
            await task
    assert calls == []


async def test_429_retry_after_then_success():
    clock = Clock()
    calls = []

    def handle(request):
        calls.append(clock())
        return (
            httpx.Response(429, headers={"Retry-After": "40"})
            if len(calls) == 1
            else httpx.Response(200, json={})
        )

    async with APIClient(
        "https://neo.example",
        transport=httpx.MockTransport(handle),
        clock=clock,
        sleep=clock.sleep,
        interval=0,
    ) as api:
        await api.request("GET", "/test")
    assert calls[1] >= 40


async def test_retry_limit_five_requests():
    clock = Clock()
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(503)

    async with APIClient(
        "https://neo.example",
        transport=httpx.MockTransport(handle),
        clock=clock,
        sleep=clock.sleep,
        interval=0,
    ) as api:
        with pytest.raises(RequestFailed):
            await api.request("GET", "/test")
    assert len(calls) == 5


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_get_merge_redirect(status):
    calls = []

    def handle(request):
        calls.append(request)
        return (
            httpx.Response(status, headers={"Location": "/new"})
            if len(calls) == 1
            else httpx.Response(200, json={})
        )

    async with APIClient(
        "https://neo.example", "secret", transport=httpx.MockTransport(handle), interval=0
    ) as api:
        response = await api.request("GET", "/old")
    assert response.url.path == "/new"
    assert calls[-1].headers["Authorization"] == "Bearer secret"


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.example/steal",
        "http://neo.example/down",
        "//evil.example/x",
        "https://name:pass@neo.example/x",
    ],
)
async def test_no_token_sent_across_redirect(target):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": target})

    async with APIClient(
        "https://neo.example", "secret", transport=httpx.MockTransport(handle), interval=0
    ) as api:
        with pytest.raises(AppError):
            await api.request("GET", "/old")
    assert len(calls) == 1


@pytest.mark.parametrize("status", [307, 308])
async def test_write_merge_requires_re_read(status):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(
            status, headers={"Location": f"/api/me/shelf/item/{catalog_item(2)['uuid']}"}
        )

    async with NeoDB(
        "https://neo.example", "secret", transport=httpx.MockTransport(handle), interval=0
    ) as api:
        assert not await api.write_shelf(catalog_item(), {"shelf_type": "complete"})
    assert len(calls) == 1


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_errors_stop_immediately(status):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status)

    async with APIClient(
        "https://neo.example", transport=httpx.MockTransport(handle), interval=0
    ) as api:
        with pytest.raises(AuthError):
            await api.request("GET", "/test")
    assert len(calls) == 1


@pytest.mark.parametrize("excluded,wrong_type", [(False, False), (True, False), (False, True)])
async def test_hyphen_query_fallback_keeps_original_exact_match(excluded, wrong_type):
    title = "系列 -第二部-"
    target = catalog_item(1)
    target.update(title="English title", type="Movie" if wrong_type else "Game")
    target["localized_title"] = [{"lang": "zh-cn", "text": title}]
    other = catalog_item(2)
    other.update(title="系列 -第三部-", type="Game")
    queries = []

    def handle(request):
        query = request.url.params["query"]
        queries.append(query)
        hits = [target, other] if query == "系列 第二部" else [other]
        return httpx.Response(200, json={"data": hits})

    async with NeoDB(
        "https://neo.example", transport=httpx.MockTransport(handle), interval=0
    ) as api:
        result = await api.search_exact(
            [title], {"game"}, exclude=[target["uuid"]] if excluded else [], category="game"
        )
    assert queries[:2] == [title, "系列 第二部"]
    if excluded or wrong_type:
        assert result is None
    else:
        assert result["uuid"] == target["uuid"]


async def test_pause_interrupts_backoff():
    cancel = asyncio.Event()

    async def sleep(seconds):
        cancel.set()

    async with APIClient(
        "https://neo.example",
        cancel=cancel,
        sleep=sleep,
        interval=0,
        transport=httpx.MockTransport(lambda r: httpx.Response(429)),
    ) as api:
        with pytest.raises(Cancelled):
            await api.request("GET", "/test")


async def test_pagination_and_raw_fields(server):
    server.sources = [collection(n) for n in range(1, 102)]
    async with server.bgm("token") as bgm:
        pages = [p async for p in bgm.pages("bgm-user")]
    assert [len(p["data"]) for p in pages] == [50, 50, 1]
    assert pages[0]["unknown_field"] == {"keep": True}
    assert all(r.headers["User-Agent"] == USER_AGENT for r in server.requests)


async def test_pagination_duplicates_fail():
    async with Bangumi(
        "token",
        interval=0,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"data": [collection()] * 50})
        ),
    ) as bgm:
        with pytest.raises(AppError):
            _ = [p async for p in bgm.pages("u")]


async def test_oauth_state_is_bound_one_use_and_no_secret_in_authorize_url(store):
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.path == "/api/v1/apps":
            return httpx.Response(
                200, json={"client_id": "app-id", "client_secret": "SUPER-SECRET"}
            )
        return httpx.Response(200, json={"access_token": "new-token"})

    def factory(base):
        return APIClient(base, interval=0, transport=httpx.MockTransport(handle))

    oauth = OAuth(store, factory)
    url = await oauth.begin("https://neo.example", "session")
    assert "SUPER-SECRET" not in url
    state = oauth.pending["state"]
    with pytest.raises(AppError):
        await oauth.finish(state, "code", "different-session")
    assert len(calls) == 1
    result = await oauth.finish(state, "code", "session")
    assert result["token"] == "new-token"
    with pytest.raises(AppError):
        await oauth.finish(state, "code", "session")
