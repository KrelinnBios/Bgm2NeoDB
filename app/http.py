import asyncio
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

import httpx

from app.config import USER_AGENT
from app.errors import AppError, AuthError, Cancelled, DeadlineExceeded, RequestFailed

REDIRECTS = {301, 302, 303, 307, 308}
TRANSIENT = {429, 500, 502, 503, 504}


def retry_after(response):
    value = response.headers.get("Retry-After", "0")
    try:
        return max(0, float(value))
    except ValueError:
        try:
            return max(
                0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
            )
        except (TypeError, ValueError, OverflowError):
            return 0


def origin(url: str) -> tuple:
    p = urlsplit(url)
    return p.scheme, p.hostname, p.port or (443 if p.scheme == "https" else 80)


def same_origin_url(base: str, target: str) -> str:
    url = urljoin(base, target)
    p = urlsplit(url)
    if p.username or p.password or origin(url) != origin(base) or p.fragment:
        raise AppError("服务器返回了其他实例或不安全的跳转地址，已停止请求。")
    return url


def response_json(response: httpx.Response) -> dict:
    try:
        result = response.json()
    except (ValueError, UnicodeError):
        raise AppError("服务器返回的数据格式不正确。") from None
    if not isinstance(result, dict):
        raise AppError("服务器返回的数据结构不正确。")
    return result


class APIClient:
    def __init__(
        self,
        base,
        token=None,
        *,
        transport=None,
        cancel=None,
        sleep=asyncio.sleep,
        clock=time.monotonic,
        interval=0.05,
    ):
        self.base = base.rstrip("/")
        self.cancel = cancel or asyncio.Event()
        self.sleep = sleep
        self.clock = clock
        self.interval = interval
        self._slot = asyncio.Lock()
        self._next_request = 0
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.client = httpx.AsyncClient(
            headers=headers,
            timeout=30,
            follow_redirects=False,
            transport=transport,
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.client.aclose()

    def checkpoint(self, deadline=None):
        if self.cancel.is_set():
            raise Cancelled()
        if deadline is not None and self.clock() >= deadline:
            raise DeadlineExceeded()

    async def pause(self, seconds, deadline=None):
        # Short waits make pause/shutdown responsive, even during a 120-second backoff.
        remaining = seconds
        while remaining > 0:
            self.checkpoint(deadline)
            chunk = min(remaining, 0.5)
            if deadline is not None:
                chunk = min(chunk, max(0, deadline - self.clock()))
            await self.sleep(chunk)
            remaining -= chunk
        self.checkpoint(deadline)

    async def request(
        self,
        method,
        path,
        *,
        params=None,
        json=None,
        data=None,
        allowed=(200,),
        follow=True,
        deadline=None,
        retry=True,
    ):
        url = same_origin_url(self.base, path)
        redirects = 0
        failures = 0
        while True:
            self.checkpoint(deadline)
            async with self._slot:
                self.checkpoint(deadline)
                gap = self._next_request - self.clock()
                if gap > 0:
                    await self.pause(gap, deadline)
                # Space request starts; network latency must not serialize all workers.
                self._next_request = self.clock() + self.interval
            timeout = 30 if deadline is None else min(30, max(0.01, deadline - self.clock()))
            try:
                response = await self.client.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    data=data,
                    timeout=timeout,
                )
            except httpx.RequestError:
                if not retry or failures >= 4:
                    raise RequestFailed() from None
                response = None
            if response is not None:
                if response.status_code in (401, 403):
                    raise AuthError()
                if response.status_code in REDIRECTS and follow:
                    if method != "GET" and response.status_code not in (307, 308):
                        raise AppError("写入请求遇到不支持的跳转，已停止。")
                    target = response.headers.get("Location")
                    if not target:
                        target = response_json(response).get("url")
                    if not isinstance(target, str) or not target or redirects >= 8:
                        raise AppError("服务器跳转无效或次数过多。")
                    url = same_origin_url(self.base, urljoin(str(response.url), target))
                    params = None
                    redirects += 1
                    continue
                if response.status_code in allowed:
                    return response
                if response.status_code not in TRANSIENT or not retry or failures >= 4:
                    raise RequestFailed(response.status_code, retry_after(response))
            delay = min(15 * 2**failures, 120) + random.uniform(0, 1)
            if response is not None:
                delay = max(delay, retry_after(response))
            failures += 1
            await self.pause(delay, deadline)
