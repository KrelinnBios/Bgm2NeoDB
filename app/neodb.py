import asyncio
import re
from urllib.parse import urlsplit

from app.errors import AppError, DeadlineExceeded
from app.http import APIClient, response_json, retry_after, same_origin_url
from app.models import bangumi_url, normalized_mark, normalized_title


def title_keys(text):
    if not isinstance(text, str):
        return set()
    keys = set()
    normalized = normalized_title(text)
    if normalized:
        keys.add(normalized)
    for part in re.split(r"[/／|]", text):
        piece = normalized_title(part)
        if piece:
            keys.add(piece)
    return keys


# Authenticated NeoDB catalog/fetch holds a ~3s per-user lock; in-progress URL
# polls return 429 until the item appears, not another 202. Spacing below that
# lock just trades real requests for 429 backoff, so keep it at the lock width.
FETCH_SPACING = 3.0


def item_uuid(value):
    # NeoDB supports both UUID and its shortuuid representation; never derive it from a title.
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9-]{8,64}", value):
        raise AppError("NeoDB 条目编号格式无法确认。")
    return value


class NeoDB(APIClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._fetch_gate = asyncio.Lock()
        self._next_fetch = 0
        self._fetch_retry_at = {}
        self.last_fetch_retry_after = 15.0

    async def me(self):
        user = response_json(await self.request("GET", "/api/me"))
        if not isinstance(user.get("url"), str) or not user["url"]:
            raise AppError("NeoDB 没有返回有效的账号信息。")
        return {k: user[k] for k in ("url", "display_name", "external_acct") if k in user}

    async def check_capabilities(self):
        schema = response_json(await self.request("GET", "/api/openapi.json"))
        try:
            path = schema["paths"]["/api/me/shelf/item/{item_uuid}"]
            ref = path["post"]["requestBody"]["content"]["application/json"]["schema"]["$ref"]
            fields = schema["components"]["schemas"][ref.rsplit("/", 1)[-1]]["properties"]
            if (
                not {
                    "shelf_type",
                    "visibility",
                    "rating_grade",
                    "comment_text",
                    "tags",
                    "post_to_fediverse",
                }
                <= fields.keys()
            ):
                raise KeyError()
            if fields["visibility"].get("maximum") != 2:
                raise KeyError()
            if "/api/catalog/fetch" not in schema["paths"] or "get" not in path:
                raise KeyError()
        except (KeyError, TypeError):
            raise AppError("此 NeoDB 实例的收藏接口不兼容，已停止迁移。") from None

    def parse_item(self, data):
        uid = item_uuid(data.get("uuid"))
        url = data.get("url")
        api_url = data.get("api_url")
        if not isinstance(url, str) or not isinstance(api_url, str):
            raise AppError("NeoDB 没有返回完整的条目信息。")
        titles = []
        for value in (data.get("title"), data.get("display_title"), data.get("orig_title")):
            if isinstance(value, str) and value.strip():
                titles.append(value)
        localized = data.get("localized_title")
        if isinstance(localized, list):
            for label in localized:
                if (
                    isinstance(label, dict)
                    and isinstance(label.get("text"), str)
                    and label["text"].strip()
                ):
                    titles.append(label["text"])
        title = data.get("title") or "未命名作品"
        return {
            "uuid": uid,
            "url": same_origin_url(self.base, url),
            "api_url": same_origin_url(self.base, api_url),
            "title": title,
            "type": data.get("type"),
            "titles": list(dict.fromkeys(titles)) or [title],
            "metadata": {
                key: value
                for key in (
                    "release_date",
                    "year",
                    "release_year",
                    "season_number",
                    "episode_count",
                    "director",
                    "actor",
                    "developer",
                    "publisher",
                    "platform",
                    "description",
                    "brief",
                    "orig_title",
                )
                if (value := data.get(key)) is not None
                and (
                    isinstance(value, str)
                    or type(value) is int
                    or isinstance(value, list)
                    and all(isinstance(v, str) for v in value)
                )
            },
            "external_resources": [
                {"url": entry["url"]}
                for entry in (data.get("external_resources") or [])
                if isinstance(entry, dict) and isinstance(entry.get("url"), str)
            ],
        }

    async def search(self, query, *, retry=True, category=None):
        items, _, _ = await self.search_page(query, retry=retry, category=category)
        return items

    async def search_page(self, query, *, retry=False, category=None, page=1):
        params = {"query": query}
        if page != 1:
            params["page"] = page
        if category:
            params["category"] = category
        response = await self.request(
            "GET", "/api/catalog/search", params=params, allowed=(200,), retry=retry
        )
        payload = response_json(response)
        data = payload.get("data")
        if not isinstance(data, list):
            raise AppError("NeoDB 搜索结果格式不正确。")
        items = []
        for hit in data:
            if not isinstance(hit, dict):
                continue
            try:
                items.append(self.parse_item(hit))
            except AppError:
                continue
        pages = payload.get("pages", 1)
        if type(pages) is not int or pages < 0:
            raise AppError("NeoDB 搜索分页格式不正确。")
        return items, pages, len(items) == len(data)

    @staticmethod
    def exact_match(items, titles, neodb_types, exclude=()):
        wanted = set()
        for title in titles:
            wanted |= title_keys(title)
        if not wanted:
            return None
        accepted_types = {t.casefold() for t in neodb_types}
        excluded = set(exclude)

        def names_of(item):
            return item.get("titles") or [item["title"]]

        def keys_of(item):
            keys = set()
            for name in names_of(item):
                keys |= title_keys(name)
            return keys

        def type_ok(item):
            return not accepted_types or (item["type"] or "").casefold() in accepted_types

        def full_hit(item):
            return bool({normalized_title(name) for name in names_of(item)} & wanted)

        matches = [it for it in items if it["uuid"] not in excluded and keys_of(it) & wanted]
        if not matches:
            return None
        full = [it for it in matches if full_hit(it)]
        typed_full = [it for it in full if type_ok(it)]
        if len(typed_full) == 1:
            return typed_full[0]
        typed = [it for it in matches if type_ok(it)]
        if len(typed) == 1:
            return typed[0]
        if not accepted_types and len(full) == 1:
            return full[0]
        if not accepted_types and len(matches) == 1:
            return matches[0]
        return None

    async def search_exact(self, titles, neodb_types, exclude=(), category=None):
        titles = list(titles)

        async def lookup(cat):
            queries = list(titles)
            # NeoDB interprets '-' in queries as search syntax. Retry literal
            # title words, but still validate hits against the original titles.
            queries.extend(" ".join(title.replace("-", " ").split()) for title in titles if title)
            seen = set()
            for title in queries:
                if not title:
                    continue
                if title in seen:
                    continue
                seen.add(title)
                items = await self.search(title, category=cat)
                match = self.exact_match(items, titles, neodb_types, exclude)
                if match:
                    return match
            return None

        match = await lookup(category)
        if match or not category:
            return match
        return await lookup(None)

    async def resolve_item_url(self, url, *, timeout=120, wait=True):
        """Resolve a user-selected item/source URL through the connected NeoDB instance."""
        url = url.strip()
        try:
            parsed = urlsplit(url)
            valid = (
                parsed.scheme in {"http", "https"}
                and parsed.hostname
                and not parsed.username
                and not parsed.password
                and parsed.port != 0
                and not any(c.isspace() or ord(c) < 32 or c == "\\" for c in url)
            )
        except ValueError:
            valid = False
        if not valid:
            raise AppError("请粘贴完整的 http:// 或 https:// 作品链接，不要包含账号密码。")
        try:
            # Source URLs are query data only. Requests and redirects still stay
            # on the connected instance, so its bearer token never goes to a source site.
            async with asyncio.timeout(timeout):
                return await self._resolve_url(url, wait=wait, timeout=timeout)
        except (DeadlineExceeded, TimeoutError):
            raise AppError(
                f"NeoDB 在 {timeout} 秒内尚未完成抓取，未保存映射或迁移收藏。"
                "可以稍后使用同一链接重试，或换一个该实例支持的作品来源链接。"
            ) from None

    async def item_from_url(self, url):
        """Accept a NeoDB item page (/category/uuid) as a validated item."""
        same_origin_url(self.base, url)
        segments = [s for s in urlsplit(url).path.split("/") if s]
        supported = len(segments) == 2 or len(segments) == 3 and segments[:2] == ["tv", "season"]
        try:
            if supported:
                item_uuid(segments[-1])
        except AppError:
            raise AppError(
                "请粘贴 NeoDB 站内条目的链接，例如 https://neodb.social/game/条目编号。"
            ) from None
        if supported:
            api_url = same_origin_url(self.base, "/api/" + "/".join(segments))
            return self.parse_item(response_json(await self.request("GET", api_url)))
        raise AppError("请粘贴 NeoDB 站内条目的链接，例如 https://neodb.social/game/条目编号。")

    async def resolve(self, subject_id, *, wait=True, timeout=30):
        return await self._resolve_url(bangumi_url(subject_id), wait=wait, timeout=timeout)

    async def _resolve_url(self, url, *, wait=True, timeout=30):
        try:
            deadline = self.clock() + timeout
            while True:
                # Wait outside the global gate so one pending source cannot hold
                # up other URLs. Keep the cooldown across quick and final passes.
                gap = self._fetch_retry_at.get(url, 0) - self.clock()
                if gap > 0:
                    await self.pause(gap, deadline)
                async with self._fetch_gate:
                    gap = self._next_fetch - self.clock()
                    if gap > 0:
                        await self.pause(gap, deadline)
                    self._next_fetch = self.clock() + FETCH_SPACING
                # Release lock before actual request so other tasks can prepare
                response = await self.request(
                    "GET",
                    "/api/catalog/fetch",
                    params={"url": url},
                    allowed=(200, 202, 404, 422, 429),
                    deadline=deadline,
                    retry=wait,
                )
                if response.status_code in (202, 429):
                    delay = max(15, retry_after(response))
                    self.last_fetch_retry_after = delay
                    self._fetch_retry_at[url] = self.clock() + delay
                    if not wait:
                        return None
                    continue
                self._fetch_retry_at.pop(url, None)
                if response.status_code == 404:
                    raise AppError("NeoDB 未找到该条目。")
                if response.status_code == 422:
                    raise AppError(
                        "NeoDB 不支持该来源或链接格式不正确，请换一个该实例支持的作品来源链接。"
                    )
                return self.parse_item(response_json(response))
        except DeadlineExceeded:
            raise DeadlineExceeded(timeout) from None

    async def refresh_item(self, item):
        return self.parse_item(response_json(await self.request("GET", item["api_url"])))

    async def shelf(self, item, *, retry=True):
        response = await self.request(
            "GET",
            f"/api/me/shelf/item/{item_uuid(item['uuid'])}",
            allowed=(200, 404),
            retry=retry,
        )
        if response.status_code == 404:
            # A merged target with no mark still redirects before returning 404.
            uid = urlsplit(str(response.url)).path.rstrip("/").rsplit("/", 1)[-1]
            if uid != item["uuid"]:
                item = await self.refresh_item(item)
            return item, None
        mark = response_json(response)
        normalized_mark(mark)
        return self.parse_item(mark["item"]), mark

    async def write_shelf(self, item, payload):
        # Do not automatically replay after a merge or uncertain POST. The migrator
        # re-reads and re-plans before another write so skipped fields stay protected.
        response = await self.request(
            "POST",
            f"/api/me/shelf/item/{item_uuid(item['uuid'])}",
            json=payload,
            allowed=(200, 307, 308),
            follow=False,
            retry=False,
        )
        if response.status_code in (307, 308):
            target = response.headers.get("Location") or response_json(response).get("url")
            if not isinstance(target, str):
                raise AppError("NeoDB 合并跳转地址无效。")
            target = same_origin_url(self.base, target)
            if not re.fullmatch(r"/api/me/shelf/item/[A-Za-z0-9-]{8,64}", urlsplit(target).path):
                raise AppError("NeoDB 合并跳转的接口不匹配。")
            return False
        return True
