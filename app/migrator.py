import asyncio
import logging
import uuid
from collections import Counter, defaultdict
from pathlib import Path

from app.bangumi import Bangumi
from app.database import Database, now, profile_id
from app.errors import AppError, AuthError, Cancelled, DeadlineExceeded, RequestFailed
from app.models import (
    STATUS_MAP,
    normalized_mark,
    plan_collection,
)
from app.neodb import NeoDB
from app.resolution import decision, inspect_candidates

FAILURES = {"resolve_failed", "failed", "partial", "conflict", "blocked_private_visibility"}

# 快速轮先搜索已有条目，抓取中的来源延后处理；最终轮共享等待预算。
RESOLVE_QUICK_TIMEOUT = 30
RESOLVE_FINAL_TIMEOUT = 60

# 已登录用户的 catalog/fetch 有约 3 秒的服务端串行锁，抓取中的地址返回 429。
# 并发调高只会让请求互相推进退避，反而更慢，所以解析并发保持在个位数；
# 写入走 shelf 接口，不受该锁限制，可以稍高。
RESOLVE_CONCURRENCY = 6
WRITE_CONCURRENCY = 10


class Migrator:
    def __init__(self, data, credentials, bangumi_factory=Bangumi, neodb_factory=NeoDB):
        self.data = Path(data)
        self.db = Database(self.data / "migration.db")
        self.credentials = credentials
        self.bangumi_factory = bangumi_factory
        self.neodb_factory = neodb_factory
        self.task = None
        self.cancel = asyncio.Event()
        self.import_date = True
        self._bgm = None
        self._resolutions = {}
        self.job = {
            "running": False,
            "kind": "idle",
            "done": 0,
            "total": 0,
            "title": "",
            "message": "",
        }
        self.pid = None
        self.log = logging.getLogger(f"bgm2neodb.{id(self)}")
        self.log.setLevel(logging.INFO)
        self.log.propagate = False
        handler = logging.FileHandler(self.data / "migration.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        self.log.addHandler(handler)

    def accounts(self):
        bgm, neo = self.credentials.get("bangumi"), self.credentials.get("neodb")
        return bgm, neo

    def select_profile(self):
        bgm, neo = self.accounts()
        if not bgm or not neo:
            self.pid = None
            return None
        self.pid = profile_id(bgm["user"]["id"], neo["instance"], neo["user"]["url"])
        self.db.profile(
            self.pid, {"bangumi": bgm["user"], "neodb": neo["user"], "instance": neo["instance"]}
        )
        return self.pid

    def assert_idle(self):
        if self.task and not self.task.done():
            raise AppError("当前操作仍在进行，请先暂停或等待完成。")

    def start(self, kind, import_date=True):
        self.assert_idle()
        if kind not in {"scan", "preview", "retry", "migrate", "auto"}:
            raise AppError("未知操作。")
        if not self.select_profile():
            raise AppError("请先连接两个账号。")
        if kind not in {"scan", "auto"} and not self.db.profile(self.pid)["export_complete"]:
            raise AppError("请先扫描 Bangumi 收藏。")
        self.cancel = asyncio.Event()
        self.import_date = import_date
        self.job = {
            "running": True,
            "kind": kind,
            "done": 0,
            "total": 0,
            "title": "",
            "message": "正在连接账号…",
        }
        self.task = asyncio.create_task(self.run(kind))

    def start_item(self, sid, import_date=True):
        self.assert_idle()
        if not self.select_profile():
            raise AppError("请先连接两个账号。")
        if not self.db.profile(self.pid)["export_complete"]:
            raise AppError("请先扫描 Bangumi 收藏。")
        if not self.db.rows(self.pid, subject_id=sid):
            raise AppError("没有找到该条目。")
        self.cancel = asyncio.Event()
        self.import_date = import_date
        self.job = {
            "running": True,
            "kind": "item",
            "done": 0,
            "total": 1,
            "title": "",
            "message": "正在处理当前条目…",
        }
        self.task = asyncio.create_task(self.run("item", sid))

    def pause(self):
        self.cancel.set()

    async def close(self):
        self.cancel.set()
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                self.job["running"] = False
        for handler in list(self.log.handlers):
            handler.close()
            self.log.removeHandler(handler)

    async def verify_accounts(self, bgm, neo, source, target):
        current_bgm = await bgm.me()
        current_neo = await neo.me()
        if current_bgm["id"] != source["user"]["id"] or current_neo["url"] != target["user"]["url"]:
            raise AppError("连接对应的账号发生变化，请重新连接两个账号。")
        await neo.check_capabilities()

    async def run(self, kind, subject_id=None):
        source, target = self.accounts()
        self.log.info("profile=%s operation=%s started", self.pid, kind)
        try:
            async with (
                self.bangumi_factory(source["token"], cancel=self.cancel) as bgm,
                self.neodb_factory(target["instance"], target["token"], cancel=self.cancel) as neo,
            ):
                await self.verify_accounts(bgm, neo, source, target)
                self._bgm = bgm
                if kind == "scan" or (
                    kind == "auto" and not self.db.profile(self.pid)["export_complete"]
                ):
                    await self.scan(bgm, source["user"])
                await self.preview_and_migrate(neo, subject_id)
                remaining = self.report()["needs_attention"]
                item_succeeded = (
                    kind == "item"
                    and self.db.rows(self.pid, subject_id=subject_id)[0]["status"] == "migrated"
                )
                self.job["message"] = (
                    "当前条目已处理并完成核对。"
                    if item_succeeded
                    else "当前条目尚未迁移成功，请查看详情中的原因。"
                    if kind == "item"
                    else f"本轮自动迁移结束，仍有 {remaining} 个条目需要处理；临时错误可继续重试，其余请查看候选并确认。"
                    if remaining
                    else "自动迁移完成，所有条目均已写入并核对。"
                )
        except (AppError, asyncio.CancelledError) as error:
            self.job["message"] = (
                str(error) if isinstance(error, AppError) else "操作已暂停，可以继续。"
            )
        except Exception as error:
            # Never log exception text, response bodies, OAuth codes or credentials.
            self.log.error("unexpected_error=%s", type(error).__name__)
            self.job["message"] = "发生内部错误，已停止操作。请查看本地日志中的错误类型。"
        finally:
            self._bgm = None
            self.job["running"] = False
            self.report()
            self.log.info("profile=%s operation=%s stopped", self.pid, kind)

    async def scan(self, bgm, user):
        scan = uuid.uuid4().hex
        sources = []
        self.job["message"] = "正在扫描 Bangumi 收藏…"
        async for page in bgm.pages(user["username"]):
            sources.extend(page["data"])
            self.job["done"] += len(page["data"])
            self.job["total"] = page.get("total", self.job["done"])
        self.db.import_snapshot(self.pid, scan, sources)

    async def preview(self, neo, only_failures=False):
        rows = self.db.rows(self.pid)
        if only_failures:
            rows = [r for r in rows if r["status"] in FAILURES | {"pending", "writing"}]
        self.job.update(done=0, total=len(rows), message="正在解析作品并读取 NeoDB 收藏…")
        limit = asyncio.Semaphore(64)
        stop = asyncio.Event()
        fatal = None

        async def process_row(row):
            nonlocal fatal
            async with limit:
                if stop.is_set():
                    return
                neo.checkpoint()
                sid, source = row["subject_id"], row["source"]
                self.job["title"] = (
                    (source.get("subject") or {}).get("name_cn")
                    or (source.get("subject") or {}).get("name")
                    or str(sid)
                )
                stage = "resolve"
                self.db.update(
                    self.pid, sid, status="pending", plan=None, attempts=row["attempts"] + 1
                )
                try:
                    # Validate privacy and data before contacting catalog/fetch.
                    plan_collection(source, None, self.import_date)
                    item = (
                        await neo.refresh_item(row["item"])
                        if row["item"]
                        else await neo.resolve(sid)
                    )
                    self.db.update(self.pid, sid, item=item)
                    stage = "preview"
                    item, current = await neo.shelf(item)
                    plan = plan_collection(source, current, self.import_date)
                    status = "ready"
                    self.db.update(
                        self.pid, sid, item=item, plan=plan, status=status, stage="", error=""
                    )
                except (AuthError, Cancelled) as error:
                    fatal = error
                    stop.set()
                    raise
                except AppError as error:
                    status = "resolve_failed" if stage == "resolve" else "failed"
                    if type(source.get("private")) is not bool:
                        status = "blocked_private_visibility"
                    self.db.update(self.pid, sid, status=status, stage=stage, error=str(error))
                    self.log.info("subject=%s stage=%s status=%s", sid, stage, status)
                finally:
                    self.job["done"] += 1

        results = await asyncio.gather(*[process_row(row) for row in rows], return_exceptions=True)
        if fatal:
            raise fatal
        for result in results:
            if isinstance(result, Exception) and not isinstance(result, AppError):
                raise result
        self.block_collisions()

    def block_collisions(self):
        groups = defaultdict(list)
        for row in self.db.rows(self.pid):
            if row["item"]:
                groups[row["item"]["uuid"]].append(row)
        for group in groups.values():
            if len(group) > 1:
                for row in group:
                    self.db.update(
                        self.pid,
                        row["subject_id"],
                        status="conflict",
                        stage="mapping",
                        error="多个 Bangumi 收藏对应同一 NeoDB 作品，已阻止自动覆盖，请在 NeoDB 手工处理。",
                    )

    def take_claims(self):
        """自动匹配时避免多个 Bangumi 条目写入同一 NeoDB 作品。

        已确认迁移的行优先保留，重复映射保留证据并要求用户处理。
        """
        claims, contested = {}, []
        for row in sorted(self.db.rows(self.pid), key=lambda r: r["status"] != "migrated"):
            if (
                row["status"] != "migrated"
                and (row.get("resolution") or {}).get("basis") == "external_shelf"
            ):
                # Older versions inferred identity from a saved shelf mark.
                # Unfinished rows must resolve again using actual matching evidence.
                self.db.update(
                    self.pid,
                    row["subject_id"],
                    item=None,
                    plan=None,
                    resolution=None,
                    status="pending",
                    stage="",
                    error="",
                )
                continue
            if not row["item"]:
                continue
            sid = row["subject_id"]
            if claims.setdefault(row["item"]["uuid"], sid) != sid:
                contested.append(row)
        for row in contested:
            sid = row["subject_id"]
            if (row.get("resolution") or {}).get("basis") == "manual":
                continue
            result = dict(row.get("resolution") or {})
            result.update(code="ambiguous", retryable=False, message="请确认要使用的作品或版本。")
            self.db.update(
                self.pid,
                sid,
                plan=None,
                status="conflict",
                stage="mapping",
                error=result["message"],
                resolution=result,
            )
        return claims

    async def claim_item(self, neo, sid, subject, item, claims, lock, allow_selected=False):
        """登记自动匹配结果；用户明确选择的目标可以覆盖已有映射。"""
        async with lock:
            owner = claims.get(item["uuid"])
            if allow_selected or owner is None or owner == sid:
                claims[item["uuid"]] = sid
                return item
            return None

    async def inspect_entry(self, neo, source, exclude=(), bgm=None, force=False):
        sid = source["subject_id"]
        row = next(iter(self.db.rows(self.pid, subject_id=sid)), {})
        detail = row.get("subject_detail")
        previous = row.get("resolution") or {}
        if not force and previous.get("basis") == "manual" and row.get("item"):
            return previous
        try:
            async with asyncio.timeout(RESOLVE_QUICK_TIMEOUT):
                if detail is None and bgm is not None:
                    detail = await bgm.subject(sid)
                    if detail is not None:
                        self.db.update(self.pid, sid, subject_detail=detail)
                result = await inspect_candidates(neo, source, detail, exclude)
        except (AuthError, Cancelled):
            raise
        except (RequestFailed, DeadlineExceeded, TimeoutError) as error:
            retryable = not isinstance(error, RequestFailed) or error.status in (
                None,
                408,
                429,
                500,
                502,
                503,
                504,
            )
            result = decision(
                "transient" if retryable else "request_rejected",
                "获取条目信息或搜索失败，可稍后重试。"
                if retryable
                else "条目或搜索请求被拒绝，请查看候选或检查实例兼容性。",
                previous.get("candidates", []),
                retryable=retryable,
            )
        except AppError:
            result = decision(
                "invalid_response",
                "条目或候选数据格式无法确认，请重新查找或手动选择。",
                previous.get("candidates", []),
            )
        result["checked_at"] = now()
        self.db.update(self.pid, sid, resolution=result)
        self._resolutions[sid] = result
        return result

    async def lookup_by_title(self, neo, source, exclude=(), force=False, bgm=None):
        sid = source["subject_id"]
        cached = self._resolutions.get(sid)
        if not force and cached and not cached.get("retryable"):
            item = cached.get("item")
            return item if item and item["uuid"] not in exclude else None
        result = await self.inspect_entry(neo, source, exclude, bgm or self._bgm, force=force)
        return result.get("item")

    async def preview_and_migrate(self, neo, subject_id=None):
        """Write quick matches immediately, then retry deferred resolutions once."""
        claims = self.take_claims()
        self._resolutions = {}
        claim_lock = asyncio.Lock()
        rows = [
            row
            for row in self.db.rows(self.pid)
            if subject_id is None or row["subject_id"] == subject_id
        ]
        rows = [
            row
            for row in rows
            if row["status"] != "migrated"
            and (
                row["status"] not in FAILURES
                or row["status"] in {"partial", "failed"}
                and row["item"]
                or not row.get("resolution")
                or row["resolution"].get("retryable", True)
                and (
                    row["status"] not in {"conflict", "blocked_private_visibility"}
                    or row.get("resolution", {}).get("basis") == "manual"
                )
            )
        ]
        deferred = {row["subject_id"] for row in rows if row["status"] in FAILURES}
        first = rows
        first.sort(key=lambda row: not bool(row["item"]))
        stop = asyncio.Event()
        fatal = None
        limit = asyncio.Semaphore(RESOLVE_CONCURRENCY)
        write_limit = asyncio.Semaphore(WRITE_CONCURRENCY)
        final_deadline = None
        self.job.update(done=0, total=len(rows), message="快速解析，匹配成功后立即写入并核对…")

        async def resolve_operation(operation, quick):
            if quick:
                return await operation(RESOLVE_QUICK_TIMEOUT)
            remaining = final_deadline - neo.clock()
            if remaining <= 0:
                raise DeadlineExceeded(RESOLVE_FINAL_TIMEOUT)
            try:
                # Bound search requests too, including retries and backoff.
                async with asyncio.timeout(remaining):
                    return await operation(remaining)
            except TimeoutError:
                raise DeadlineExceeded(RESOLVE_FINAL_TIMEOUT) from None

        def resolution_timed_out(sid):
            result = dict(self._resolutions.get(sid) or {})
            result.update(
                code="fetch_timeout",
                retryable=True,
                message="本轮最终解析时间已用完，可稍后重试；已有候选已保留。",
            )
            self.db.update(
                self.pid,
                sid,
                status="resolve_failed",
                stage="resolve",
                error=f"本轮最终解析共用的 {RESOLVE_FINAL_TIMEOUT} 秒等待时间已用完，尚未确认对应条目；可稍后重试或手动选择对应条目。",
                resolution=result,
            )

        async def process(row, quick):
            nonlocal fatal
            try:
                async with limit:
                    if stop.is_set():
                        return
                    sid, source = row["subject_id"], row["source"]
                    neo.checkpoint()
                    self.job["title"] = (
                        (source.get("subject") or {}).get("name_cn")
                        or (source.get("subject") or {}).get("name")
                        or str(sid)
                    )
                    self.db.update(
                        self.pid,
                        sid,
                        status="pending",
                        plan=None,
                        stage="resolve",
                        error="",
                        attempts=row["attempts"] + 1,
                    )
                    item = None
                    try:
                        plan_collection(source, None, self.import_date)
                        if row["item"]:
                            item = await neo.refresh_item(row["item"])
                        else:
                            # Search existing items in the quick pass too, so a
                            # stalled fetch cannot hold searchable rows behind
                            # the final pass's long polling slots.
                            async with claim_lock:
                                taken = set(claims)
                            force_resolution = (
                                row["status"] in {"resolve_failed", "conflict"} and not row["item"]
                            )
                            if force_resolution:

                                def operation(timeout):
                                    return self.lookup_by_title(neo, source, taken, force=True)
                            else:

                                def operation(timeout):
                                    return self.lookup_by_title(neo, source, taken)

                            item = await resolve_operation(operation, quick)
                            if item is None:
                                result = self._resolutions.get(sid) or {}
                                if result.get("code") not in (None, "no_match", "matched"):
                                    self.db.update(
                                        self.pid,
                                        sid,
                                        status="conflict"
                                        if result["code"] == "ambiguous"
                                        else "resolve_failed",
                                        stage="resolve",
                                        error=result["message"],
                                    )
                                    if result.get("retryable") and quick:
                                        deferred.add(sid)
                                    return
                                item = await resolve_operation(
                                    lambda timeout: neo.resolve(
                                        sid, wait=not quick, timeout=timeout
                                    ),
                                    quick,
                                )
                            if item is None:
                                deferred.add(sid)
                                return
                    except DeadlineExceeded:
                        if quick:
                            deferred.add(sid)
                            return
                        resolution_timed_out(sid)
                        return
                    except (AuthError, Cancelled):
                        raise
                    except AppError as error:
                        if type(source.get("private")) is not bool:
                            self.db.update(
                                self.pid,
                                sid,
                                status="blocked_private_visibility",
                                stage="resolve",
                                error=str(error),
                            )
                            return
                        miss = "未找到该条目" in str(error) or "不支持该来源" in str(error)
                        if quick and not miss:
                            deferred.add(sid)
                            return
                    if item is None:
                        async with claim_lock:
                            taken = set(claims)
                        try:
                            item = await resolve_operation(
                                lambda timeout: self.lookup_by_title(neo, source, taken), quick
                            )
                        except DeadlineExceeded:
                            resolution_timed_out(sid)
                            return
                        if item is None:
                            result = self._resolutions.get(sid) or decision(
                                "no_match", "未找到对应条目，请手动选择对应条目。"
                            )
                            self.db.update(
                                self.pid,
                                sid,
                                status="resolve_failed",
                                stage="resolve",
                                error=result["message"],
                                resolution=result,
                            )
                            return
                    subject = source.get("subject") or {}
                    manual_mapping = (row.get("resolution") or {}).get("basis") == "manual"
                    item = await self.claim_item(
                        neo,
                        sid,
                        subject,
                        item,
                        claims,
                        claim_lock,
                        allow_selected=manual_mapping,
                    )
                    if item is None:
                        result = dict(self._resolutions.get(sid) or row.get("resolution") or {})
                        result.update(
                            code="ambiguous", retryable=False, message="请确认要使用的作品或版本。"
                        )
                        self.db.update(
                            self.pid,
                            sid,
                            item=None,
                            plan=None,
                            status="conflict",
                            stage="mapping",
                            error="请确认要使用的作品或版本。",
                            resolution=result,
                        )
                        return
                    result = dict(self._resolutions.get(sid) or row.get("resolution") or {})
                    result.update(
                        code="matched",
                        item=item,
                        retryable=False,
                        basis=result.get("basis") or "source_url",
                        message="已确认对应作品。",
                    )
                    self.db.update(self.pid, sid, item=item, resolution=result)
                    item, current = await neo.shelf(item)
                    # shelf() 可能跟随合并跳转到另一条作品，需要重新登记映射。
                    item = await self.claim_item(
                        neo,
                        sid,
                        subject,
                        item,
                        claims,
                        claim_lock,
                        allow_selected=manual_mapping,
                    )
                    if item is None:
                        self.db.update(
                            self.pid,
                            sid,
                            item=None,
                            plan=None,
                            status="conflict",
                            stage="mapping",
                            error="作品合并后需要重新确认对应的作品或版本。",
                        )
                        return
                    plan = plan_collection(source, current, self.import_date)
                    self.db.update(
                        self.pid, sid, item=item, plan=plan, status="ready", stage="", error=""
                    )
                    if stop.is_set():
                        return
                    prepared = {**row, "item": item, "plan": plan, "status": "ready"}
                # Let another resolver proceed while this row waits for a write slot.
                await self.migrate(
                    neo,
                    rows=[prepared],
                    stop=stop,
                    limit=write_limit,
                    claims=claims,
                    claim_lock=claim_lock,
                )
            except (AppError, asyncio.CancelledError) as error:
                if fatal is None:
                    fatal = error
                stop.set()
            except Exception as error:
                if fatal is None:
                    fatal = error
                stop.set()
            finally:
                self.job["done"] += 1

        await asyncio.gather(*(process(row, True) for row in first))
        if fatal:
            raise fatal
        neo.checkpoint()
        remaining = [
            row
            for row in self.db.rows(self.pid)
            if row["subject_id"] in deferred
            and row["status"] != "migrated"
            and (
                not row.get("resolution")
                or row["resolution"].get("retryable")
                or row["resolution"].get("code") == "no_match"
                and row["status"] == "pending"
            )
        ]
        if remaining:
            import time

            final_deadline = neo.clock() + RESOLVE_FINAL_TIMEOUT
            # 前端需要Unix时间戳（秒），计算实际的截止时间
            unix_deadline = time.time() + RESOLVE_FINAL_TIMEOUT
            start_time = time.time()
            self.job.update(
                done=0,
                total=len(remaining),
                message=f"快速条目已处理，剩余条目共用最多 {RESOLVE_FINAL_TIMEOUT} 秒等待解析；已开始写入的条目会继续核对…",
                countdown_deadline=unix_deadline,
                phase_start_time=start_time,
            )
            await asyncio.gather(*(process(row, False) for row in remaining))
            if fatal:
                raise fatal
            # 清除倒计时标记
            self.job.pop("countdown_deadline", None)
            self.job.pop("phase_start_time", None)
            self.job.pop("phase_start_time", None)
        neo.checkpoint()

    async def migrate(self, neo, rows=None, stop=None, limit=None, claims=None, claim_lock=None):
        manage_progress = rows is None
        if manage_progress:
            # 只在独立迁移时统一判重；自动迁移由 claim_item 逐条登记映射，
            # 若在这里重扫全部行，会把并发中的行覆盖成 conflict。
            self.block_collisions()
            rows = [r for r in self.db.rows(self.pid) if r["status"] in {"ready", "skipped"}]
            self.job.update(done=0, total=len(rows), message="正在迁移，每项写入后都会核对结果…")
        limit = limit if limit is not None else asyncio.Semaphore(WRITE_CONCURRENCY)
        stop = stop if stop is not None else asyncio.Event()
        fatal = None

        async def process_row(row):
            nonlocal fatal
            async with limit:
                if stop.is_set():
                    return
                neo.checkpoint()
                sid = row["subject_id"]
                self.job["title"] = row["item"]["title"]
                self.db.update(
                    self.pid, sid, status="writing", stage="read", attempts=row["attempts"] + 1
                )
                wrote = False
                stage = "read"
                try:
                    item = row["item"]
                    for attempt in range(5):
                        neo.checkpoint()
                        item, current = await neo.shelf(item, retry=False)
                        self.db.update(self.pid, sid, item=item)
                        manual = (row.get("resolution") or {}).get("basis") == "manual"
                        other = self.db.has_migrated_target(self.pid, sid, item["uuid"])
                        if claims is not None:
                            claimed = await self.claim_item(
                                neo,
                                sid,
                                row["source"].get("subject") or {},
                                item,
                                claims,
                                claim_lock,
                                allow_selected=manual,
                            )
                            other = other or claimed is None
                        if other and not manual:
                            # 只跳过这一条，不中断整批：撞车的行常年排在最前，
                            # 一旦当成致命错误，后面所有条目都不会被处理。
                            self.db.update(
                                self.pid,
                                sid,
                                status="conflict",
                                stage="mapping",
                                error="该 NeoDB 作品已由另一个 Bangumi 收藏占用，请在界面手动选择对应条目。",
                            )
                            return
                        current_normal = normalized_mark(current)
                        plan = row["plan"]
                        # Even an identical mark must receive a POST. A previous uncertain
                        # POST may already have applied the approved result; retry it and
                        # require a successful response followed by verification.
                        if current_normal != plan["before"] and current_normal != plan["after"]:
                            self.db.update(
                                self.pid,
                                sid,
                                status="conflict",
                                stage="read",
                                error="NeoDB 数据在预览后发生变化，请重新预览后确认。",
                            )
                            fatal = AppError("NeoDB 数据在检查后发生变化，自动迁移已暂停。")
                            stop.set()
                            raise fatal
                        fresh = plan_collection(row["source"], current, self.import_date)
                        if stop.is_set():
                            self.db.update(self.pid, sid, status="ready", stage="", error="")
                            return
                        stage = "write"
                        self.db.update(self.pid, sid, stage=stage)
                        try:
                            # Record uncertainty before POST so interrupted requests are not reported as success.
                            wrote = True
                            success = await neo.write_shelf(item, fresh["payload"])
                        except RequestFailed:
                            # A failed write pauses the job; continuation starts by re-reading.
                            raise
                        if not success:
                            # 307/308: re-resolve, GET merged target, compare approved preview before replay.
                            item = await neo.refresh_item(item)
                            continue
                        stage = "verify"
                        self.db.update(self.pid, sid, stage=stage)
                        item, result = await neo.shelf(item, retry=False)
                        if normalized_mark(result) != plan["after"]:
                            raise AppError("写入后回读结果与预览不一致，请重新预览并重试。")
                        self.db.update(
                            self.pid,
                            sid,
                            item=item,
                            status="migrated",
                            stage="",
                            error="",
                            migrated_at=now(),
                            migrated_fields=sorted(
                                set(row["migrated_fields"] or []) | set(fresh["after"])
                            ),
                        )
                        break
                    else:
                        raise AppError("作品多次合并跳转，已停止，请重新预览。")
                except (AuthError, Cancelled) as error:
                    self.db.update(
                        self.pid,
                        sid,
                        status="partial" if wrote else "failed",
                        stage=stage,
                        error=str(error),
                    )
                    fatal = error
                    stop.set()
                    raise
                except AppError as error:
                    # Keep a specific mapping/staleness conflict visible.
                    current_status = self.db.rows(self.pid, subject_id=sid)[0]["status"]
                    if current_status != "conflict":
                        self.db.update(
                            self.pid,
                            sid,
                            status="partial" if wrote else "failed",
                            stage=stage,
                            error=str(error),
                        )
                    fatal = AppError(f"条目 {sid} 写入或核对失败，自动迁移已暂停：{error}")
                    stop.set()
                    raise fatal
                finally:
                    self.log.info("subject=%s operation=migrate checked", sid)
                    if manage_progress:
                        self.job["done"] += 1

        results = await asyncio.gather(*[process_row(row) for row in rows], return_exceptions=True)
        if fatal:
            raise fatal
        for result in results:
            if isinstance(result, Exception) and not isinstance(result, AppError):
                raise result

    def report(self):
        rows = self.db.rows(self.pid) if self.pid else []
        counts = Counter(r["status"] for r in rows)
        result = {
            "generated_at": now(),
            "profile": self.pid,
            "total": len(rows),
            "resolved": sum(bool(r["item"]) for r in rows),
            **dict(counts),
        }
        for state in (
            "pending",
            "writing",
            "ready",
            "resolve_failed",
            "migrated",
            "skipped",
            "partial",
            "failed",
            "conflict",
            "blocked_private_visibility",
        ):
            result.setdefault(state, 0)
        result["needs_attention"] = sum(counts[s] for s in FAILURES | {"writing"})
        result["retryable"] = sum(
            r["status"] != "migrated"
            and (
                not r.get("resolution")
                or r["resolution"].get("retryable", False)
                or r["status"] in {"pending", "ready", "writing", "partial", "failed"}
            )
            for r in rows
        )
        for shelf in set(STATUS_MAP.values()):
            result[shelf] = sum(STATUS_MAP.get(r["source"].get("type")) == shelf for r in rows)
        confirmed = [r for r in rows if r["status"] == "migrated" and r["plan"]]
        result["ratings_migrated"] = sum(
            "rating_grade" in r["migrated_fields"]
            and r["source"]["type"] != 4
            and bool(r["source"].get("rate"))
            for r in confirmed
        )
        result["ratings_skipped_on_hold"] = sum(r["source"]["type"] == 4 for r in rows)
        result["comments_migrated"] = sum(
            "comment_text" in r["migrated_fields"] and bool(r["source"].get("comment"))
            for r in confirmed
        )
        result["tags_migrated"] = sum(
            "tags" in r["migrated_fields"] and bool(r["source"].get("tags")) for r in confirmed
        )
        result["progress_migrated"] = 0
        result["progress_skipped"] = sum(
            bool(r["source"].get("ep_status") or r["source"].get("vol_status")) for r in rows
        )
        return result
