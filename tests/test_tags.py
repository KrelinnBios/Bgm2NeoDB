import asyncio

import httpx
import pytest

from app.errors import AppError
from app.neodb import NeoDB


async def test_tag_catalog_reads_all_pages_once_for_concurrent_callers():
    pages = [
        [{"uuid": "tag-1", "title": "Android", "visibility": 2}],
        [{"uuid": "tag-2", "title": "Web", "visibility": 0}],
    ]
    calls = []

    async def handler(request):
        calls.append(request)
        await asyncio.sleep(0)
        assert request.method == "GET"
        assert request.url.path == "/api/me/tag/"
        page = int(request.url.params["page"])
        return httpx.Response(200, json={"data": pages[page - 1], "pages": 2, "count": 2})

    async with NeoDB(
        "https://neo.example", transport=httpx.MockTransport(handler), interval=0
    ) as neo:
        results = await asyncio.gather(*(neo.tag_names() for _ in range(6)))
        assert all(names == {"android": "Android", "web": "Web"} for names in results)
        assert await neo.tag_names() == results[0]
    assert len(calls) == 2


@pytest.mark.parametrize("pages", [0, 1])
async def test_empty_tag_catalog(pages):
    async with NeoDB(
        "https://neo.example",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"data": [], "pages": pages, "count": 0})
        ),
    ) as neo:
        assert await neo.tag_names() == {}


@pytest.mark.parametrize(
    "payload",
    [
        {"data": [], "pages": 1, "count": 1},
        {"data": [], "pages": 2, "count": 101},
        {"data": [], "pages": True, "count": 0},
        {"data": [], "pages": 0, "count": -1},
        {"data": [], "pages": 0},
        {"data": ["Web"], "pages": 1, "count": 1},
        {"data": [{"uuid": "tag-1", "title": None}], "pages": 1, "count": 1},
        {"data": [{"uuid": "tag-1", "title": " "}], "pages": 1, "count": 1},
        {"data": [{"title": "Web"}], "pages": 1, "count": 1},
    ],
)
async def test_invalid_tag_catalog_is_rejected_without_caching(payload):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        data = payload if calls == 1 else {"data": [], "pages": 0, "count": 0}
        return httpx.Response(200, json=data)

    async with NeoDB(
        "https://neo.example", transport=httpx.MockTransport(handler), interval=0
    ) as neo:
        with pytest.raises(AppError, match="标签"):
            await neo.tag_names()
        assert await neo.tag_names() == {}
    assert calls == 2


@pytest.mark.parametrize("changed_count", [False, True])
async def test_tag_catalog_rejects_repeated_or_changed_pages(changed_count):
    def handler(request):
        page = int(request.url.params["page"])
        return httpx.Response(
            200,
            json={
                "data": [{"uuid": "tag-1", "title": "Web", "visibility": 0}],
                "pages": 2,
                "count": 3 if changed_count and page == 2 else 2,
            },
        )

    async with NeoDB(
        "https://neo.example", transport=httpx.MockTransport(handler), interval=0
    ) as neo:
        with pytest.raises(AppError, match="标签"):
            await neo.tag_names()
