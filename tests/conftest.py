import copy
import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

from app.auth import CredentialStore
from app.bangumi import Bangumi
from app.neodb import NeoDB

SCHEMA = json.loads((Path(__file__).parents[1] / "docs/neodb-openapi.json").read_text())


class MemoryKeyring:
    def __init__(self):
        self.values = {}

    def get_password(self, service, key):
        return self.values.get((service, key))

    def set_password(self, service, key, value):
        self.values[service, key] = value

    def delete_password(self, service, key):
        self.values.pop((service, key), None)


class Clock:
    def __init__(self):
        self.value = 0

    def __call__(self):
        return self.value

    async def sleep(self, seconds):
        self.value += seconds


def collection(sid=1, **values):
    return {
        "subject_id": sid,
        "subject_type": 2,
        "type": 2,
        "rate": 8,
        "comment": "好看！",
        "tags": [" 动画 ", "动画", ""],
        "ep_status": 12,
        "vol_status": 0,
        "private": False,
        "updated_at": "2020-01-01T00:00:00Z",
        "subject": {"id": sid, "name": f"作品 {sid}", "name_cn": f"作品 {sid}"},
        **values,
    }


def catalog_item(sid=1):
    uid = f"00000000-0000-0000-0000-{sid:012d}"
    return {
        "uuid": uid,
        "url": f"https://neo.example/tv/{uid}",
        "api_url": f"https://neo.example/api/tv/{uid}",
        "title": f"作品 {sid}",
        "type": "TVSeason",
    }


def mark(sid=1, **values):
    return {
        "shelf_type": "wishlist",
        "visibility": 0,
        "rating_grade": None,
        "comment_text": None,
        "tags": [],
        "item": catalog_item(sid),
        "created_time": "2021-01-01T00:00:00Z",
        **values,
    }


class FakeServer:
    def __init__(self):
        self.sources = [collection()]
        self.marks = {}
        self.requests = []
        self.writes = []
        self.clock = Clock()
        self.fail_write = None
        self.post_commits_then_disconnect = False
        self.auth_failure = False
        self.collision = False
        self.after_write = None
        self.search_hits = []

    def handler(self, request):
        self.requests.append(request)
        path = request.url.path
        if self.auth_failure:
            return httpx.Response(401)
        if request.url.host == "api.bgm.tv":
            if path == "/v0/me":
                return httpx.Response(
                    200, json={"id": 100, "username": "bgm-user", "nickname": "测试用户"}
                )
            if path.startswith("/v0/subjects/"):
                sid = int(path.rsplit("/", 1)[-1])
                source = next((s for s in self.sources if s["subject_id"] == sid), None)
                return (
                    httpx.Response(200, json={**source["subject"], "id": sid})
                    if source
                    else httpx.Response(404)
                )
            offset = int(request.url.params["offset"])
            return httpx.Response(
                200,
                json={
                    "data": self.sources[offset : offset + 50],
                    "total": len(self.sources),
                    "offset": offset,
                    "limit": 50,
                    "unknown_field": {"keep": True},
                },
            )
        if path == "/api/me":
            return httpx.Response(
                200, json={"url": "https://neo.example/users/user/", "display_name": "测试用户"}
            )
        if path == "/api/openapi.json":
            return httpx.Response(200, json=SCHEMA)
        if path == "/api/catalog/fetch":
            source_url = request.url.params["url"]
            if urlsplit(source_url).hostname == "neo.example":
                sid = int(urlsplit(source_url).path.rstrip("/").rsplit("-", 1)[-1])
            else:
                sid = int(source_url.rsplit("/", 1)[-1])
            item = catalog_item(1 if self.collision else sid)
            return httpx.Response(
                302, headers={"Location": item["api_url"]}, json={"url": item["api_url"]}
            )
        if path == "/api/catalog/search":
            return httpx.Response(200, json={"data": list(self.search_hits)})
        if path.startswith("/api/tv/"):
            return httpx.Response(200, json=catalog_item(int(path.rsplit("-", 1)[-1])))
        if path.startswith("/api/me/shelf/item/"):
            uid = path.rsplit("/", 1)[-1]
            sid = int(uid.rsplit("-", 1)[-1])
            if request.method == "GET":
                return (
                    httpx.Response(200, json=self.marks[uid])
                    if uid in self.marks
                    else httpx.Response(404)
                )
            assert request.method == "POST"
            self.writes.append(request)
            if self.fail_write:
                return httpx.Response(self.fail_write)
            payload = json.loads(request.content)
            self.marks[uid] = mark(sid, **payload)
            if self.after_write:
                self.after_write(self.marks[uid])
            if self.post_commits_then_disconnect:
                self.post_commits_then_disconnect = False
                raise httpx.ReadTimeout("secret should never reach logs", request=request)
            return httpx.Response(200, json={"message": "OK"})
        raise AssertionError(f"Unexpected mocked API request: {request.method} {path}")

    def bgm(self, token, **kwargs):
        return Bangumi(
            token,
            transport=httpx.MockTransport(self.handler),
            sleep=self.clock.sleep,
            clock=self.clock,
            interval=0,
            **kwargs,
        )

    def neo(self, base, token=None, **kwargs):
        return NeoDB(
            base,
            token,
            transport=httpx.MockTransport(self.handler),
            sleep=self.clock.sleep,
            clock=self.clock,
            interval=0,
            **kwargs,
        )


@pytest.fixture
def server():
    return FakeServer()


@pytest.fixture
def store():
    result = CredentialStore(backend=MemoryKeyring())
    result.put("bangumi", {"token": "BGM-SECRET", "user": {"id": 100, "username": "bgm-user"}})
    result.put(
        "neodb",
        {
            "token": "NEO-SECRET",
            "instance": "https://neo.example",
            "user": {"url": "https://neo.example/users/user/"},
        },
    )
    return result


@pytest.fixture
def source():
    return copy.deepcopy(collection())
