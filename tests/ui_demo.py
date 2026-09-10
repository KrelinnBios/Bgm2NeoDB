"""Offline browser acceptance fixture: python -m tests.ui_demo (requires dev dependencies)."""

import asyncio
import tempfile
from pathlib import Path

import uvicorn

from app.auth import CredentialStore
from app.config import HOST, PORT
from app.routes import create_app
from tests.conftest import FakeServer, MemoryKeyring, collection


def main():
    remote = FakeServer()
    remote.sources = [
        collection(1, subject={"name_cn": "星际牛仔"}),
        collection(2, type=4, rate=9, subject={"name_cn": "来自新世界"}),
        collection(3, private=True, subject={"name_cn": "私密作品 <script>示例</script>"}),
    ]
    store = CredentialStore(backend=MemoryKeyring())
    store.put("bangumi", {"token": "offline-demo", "user": {"id": 100, "username": "bgm-user"}})
    store.put(
        "neodb",
        {
            "token": "offline-demo",
            "instance": "https://neo.example",
            "user": {"url": "https://neo.example/users/user/", "display_name": "离线测试"},
        },
    )
    with tempfile.TemporaryDirectory(prefix="bgm2neodb-ui-") as folder:
        app = create_app(Path(folder), store, remote.bgm, remote.neo)
        app.state.engine.select_profile()
        asyncio.run(app.state.engine.run("scan"))
        uvicorn.run(app, host=HOST, port=PORT, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
