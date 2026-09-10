import asyncio
import hashlib
import mimetypes
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app.auth import CredentialStore, OAuth, validate_instance
from app.config import BANGUMI_TOKEN_URL, DATA, NEODB_DEFAULT, ORIGIN, ROOT
from app.errors import AppError
from app.migrator import FAILURES, Migrator
from app.models import STATUS_LABELS, bangumi_url

# Windows 注册表里 .js 有时被映射为 text/plain，强制修正
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")

STATUS_GROUPS = {
    "queued": {"pending", "ready", "writing", "skipped"},
    "resolve_failed": {"resolve_failed"},
    "conflict": {"conflict"},
    "blocked_private_visibility": {"blocked_private_visibility"},
    "failed": {"failed", "partial"},
    "migrated": {"migrated"},
}


class TokenInput(BaseModel):
    token: str = Field(min_length=1, max_length=4096)


class InstanceInput(BaseModel):
    instance: str = Field(default=NEODB_DEFAULT, min_length=1, max_length=300)


class JobOptions(BaseModel):
    import_date: bool = True


class MapInput(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


def create_app(data=DATA, credentials=None, bangumi_factory=None, neodb_factory=None):
    namespace = "Bgm2NeoDB:" + hashlib.sha256(str(data.resolve()).encode()).hexdigest()[:16]
    store = credentials or CredentialStore(namespace)
    factories = {}
    if bangumi_factory:
        factories["bangumi_factory"] = bangumi_factory
    if neodb_factory:
        factories["neodb_factory"] = neodb_factory
    engine = Migrator(data, store, **factories)
    oauth = OAuth(store)
    session = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    auth_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        engine.select_profile()
        yield
        await engine.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine = engine
    app.state.oauth = oauth
    templates = Jinja2Templates(directory=ROOT / "templates")

    @app.middleware("http")
    async def local_security(request, call_next):
        if request.headers.get("host") != ORIGIN.removeprefix("http://"):
            return JSONResponse({"error": "请从本机的 127.0.0.1:8765 打开程序。"}, status_code=403)
        if (
            request.headers.get("sec-fetch-site") == "cross-site"
            and request.url.path != "/"
            and not request.url.path.startswith("/auth/neodb/")
        ):
            return JSONResponse({"error": "不允许来自其他网站的访问。"}, status_code=403)
        public = request.url.path == "/" or request.url.path.startswith("/static/")
        if not public and not secrets.compare_digest(request.cookies.get("bgm2neodb", ""), session):
            return JSONResponse({"error": "页面连接已过期，请刷新首页。"}, status_code=403)
        if request.method not in ("GET", "HEAD"):
            if request.headers.get("origin") != ORIGIN or not secrets.compare_digest(
                request.headers.get("x-csrf-token", ""), csrf
            ):
                return JSONResponse({"error": "页面验证失败，请刷新后重试。"}, status_code=403)
            try:
                if int(request.headers.get("content-length", "0")) > 8192:
                    return JSONResponse({"error": "提交内容过长。"}, status_code=413)
            except ValueError:
                return JSONResponse({"error": "请求格式错误。"}, status_code=400)
        response = await call_next(request)
        csp_nonce = getattr(request.state, "csp_nonce", None)
        csp = f"default-src 'self'; script-src 'self'{f\" 'nonce-{csp_nonce}'\" if csp_nonce else ''}; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": csp,
            }
        )
        return response

    @app.exception_handler(AppError)
    async def app_error(request, error):
        return JSONResponse({"error": str(error)}, status_code=400)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        # FastAPI's default detail can echo submitted credentials.
        return JSONResponse({"error": "输入格式不正确，请检查后重试。"}, status_code=422)

    @app.get("/")
    async def home(request: Request):
        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce
        response = templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "csrf": csrf,
                "token_url": BANGUMI_TOKEN_URL,
                "nonce": nonce,
            },
        )
        response.set_cookie("bgm2neodb", session, httponly=True, samesite="lax")
        return response

    @app.get("/api/state")
    async def state():
        if not engine.job["running"]:
            engine.select_profile()
        bgm, neo = engine.accounts()
        return {
            "bangumi": bgm["user"] if bgm else None,
            "neodb": {"user": neo["user"], "instance": neo["instance"]} if neo else None,
            "warning": store.warning,
            "job": engine.job,
            "summary": engine.report(persist=False),
            "has_snapshot": bool(engine.pid and engine.db.profile(engine.pid)["export_complete"]),
        }

    @app.post("/api/connect/bangumi")
    async def connect_bangumi(value: TokenInput):
        async with auth_lock:
            engine.assert_idle()
            token = value.token.strip()
            if not token or any(c.isspace() for c in token):
                raise AppError("授权凭证中不应包含空格或换行。")
            async with engine.bangumi_factory(token) as api:
                user = await api.me()
            store.put("bangumi", {"token": token, "user": user})
            engine.select_profile()
            return {"ok": True}

    @app.post("/api/connect/neodb")
    async def connect_neodb(value: InstanceInput):
        async with auth_lock:
            engine.assert_idle()
            instance = await validate_instance(value.instance)
            return {"url": await oauth.begin(instance, session)}

    @app.get("/auth/neodb/callback")
    async def callback(request: Request):
        async with auth_lock:
            engine.assert_idle()
            try:
                if request.query_params.get("error"):
                    raise AppError("NeoDB 授权未完成，请返回首页重新连接。")
                result = await oauth.finish(
                    request.query_params.get("state", ""),
                    request.query_params.get("code", ""),
                    session,
                )
                async with engine.neodb_factory(result["instance"], result["token"]) as api:
                    result["user"] = await api.me()
                    await api.check_capabilities()
                store.put("neodb", result)
                engine.select_profile()
                engine.job["message"] = "NeoDB 连接成功，可以开始扫描。"
            except AppError as error:
                engine.job["message"] = str(error)
            return RedirectResponse("/", status_code=303)

    @app.post("/api/disconnect/{platform}")
    async def disconnect(platform: str):
        async with auth_lock:
            engine.assert_idle()
            if platform not in ("bangumi", "neodb"):
                raise AppError("未知平台。")
            store.clear(platform)
            oauth.pending = None
            engine.select_profile()
            return {"ok": True}

    @app.post("/api/jobs/{kind}")
    async def start_job(kind: str, options: JobOptions = JobOptions()):
        async with auth_lock:
            engine.start(kind, import_date=options.import_date)
            return {"ok": True}

    @app.post("/api/jobs/item/{sid}")
    async def start_item_job(sid: int, options: JobOptions = JobOptions()):
        async with auth_lock:
            engine.start_item(sid, import_date=options.import_date)
            return {"ok": True}

    @app.post("/api/pause")
    async def pause():
        engine.pause()
        return {"ok": True}

    @app.get("/api/entries")
    async def entries(page: int = 1, filter: str = "all", q: str = ""):
        rows = engine.db.rows(engine.pid) if engine.pid else []
        if filter.startswith("group:"):
            allowed = STATUS_GROUPS.get(filter.removeprefix("group:"), set())
            rows = [r for r in rows if r["status"] in allowed]
        elif filter.startswith("status:"):
            rows = [r for r in rows if r["status"] == filter.removeprefix("status:")]
        elif filter == "failed":
            rows = [r for r in rows if r["status"] in FAILURES | {"writing"}]
        elif filter == "ready":
            rows = [r for r in rows if r["status"] == "ready"]
        elif filter == "done":
            rows = [r for r in rows if r["status"] == "migrated"]
        result = []
        for row in rows:
            source = row["source"]
            subject = source.get("subject") or {}
            title = subject.get("name_cn") or subject.get("name") or str(row["subject_id"])
            if q and q.casefold() not in (title + str(row["subject_id"])).casefold():
                continue
            result.append(
                {
                    "id": row["subject_id"],
                    "title": title,
                    "status": row["status"],
                    "source_url": bangumi_url(row["subject_id"]),
                    "source_type": subject.get("type") or source.get("subject_type"),
                    "source_platform": subject.get("platform")
                    or (row.get("subject_detail") or {}).get("platform"),
                    "target_url": row["item"]["url"] if row["item"] else None,
                    "source_status": STATUS_LABELS.get(source.get("type"), "未知"),
                    "source_rating": source.get("rate"),
                    "private": source.get("private"),
                    "nsfw": bool((subject or {}).get("nsfw") or source.get("nsfw")),
                    "plan": row["plan"],
                    "error": row["error"],
                    "stage": row["stage"],
                    "resolution": row.get("resolution"),
                }
            )
        page = max(1, page)
        return {"total": len(result), "page": page, "entries": result[(page - 1) * 30 : page * 30]}

    def row_by_id(sid):
        if not engine.pid:
            raise AppError("请先连接账号。")
        rows = engine.db.rows(engine.pid, subject_id=sid)
        for row in rows:
            if row["subject_id"] == sid:
                return row
        raise AppError("没有找到该条目。")

    @app.get("/api/map/candidates/{sid}")
    async def map_candidates(sid: int):
        async with auth_lock:
            engine.assert_idle()
            row = row_by_id(sid)
            source, target = engine.accounts()
            if not target:
                raise AppError("请先连接 NeoDB。")
            taken = {
                r["item"]["uuid"]
                for r in engine.db.rows(engine.pid)
                if r["subject_id"] != sid and r["item"]
            }
            async with engine.neodb_factory(target["instance"], target["token"]) as neo:
                if source:
                    async with engine.bangumi_factory(source["token"]) as bgm:
                        item = await engine.lookup_by_title(
                            neo, row["source"], taken, force=True, bgm=bgm
                        )
                        result = engine._resolutions.get(sid) or {}
                else:
                    item = await engine.lookup_by_title(neo, row["source"], taken, force=True)
                    result = engine._resolutions.get(sid) or {}
            item = item or result.get("item")
            if item and not row["item"]:
                engine.db.update(
                    engine.pid, sid, item=item, status="pending", plan=None, error="", stage=""
                )
            if not row["item"] and not item:
                engine.db.update(
                    engine.pid,
                    sid,
                    status="conflict" if result["code"] == "ambiguous" else "resolve_failed",
                    error=result["message"],
                    stage="resolve",
                )
            return {
                "candidates": result.get("candidates", []),
                "exact": item["url"] if item else None,
                "resolution": result,
            }

    @app.post("/api/map/{sid}")
    async def map_entry(sid: int, value: MapInput):
        async with auth_lock:
            engine.assert_idle()
            return await save_mapping(sid, value.url)

    @app.get("/api/map/{sid}/pending")
    async def poll_pending_mapping(sid: int):
        async with auth_lock:
            engine.assert_idle()
            row = row_by_id(sid)
            resolution = row.get("resolution") or {}
            url = resolution.get("pending_url")
            if not isinstance(url, str) or not url:
                raise AppError("该条目当前没有等待中的外链抓取。")
            return await save_mapping(sid, url)

    async def save_mapping(sid, url):
        row = row_by_id(sid)
        if not engine.accounts()[1]:
            raise AppError("请先连接 NeoDB。")
        instance = engine.accounts()[1]["instance"]
        retry_after = 15
        async with engine.neodb_factory(instance, engine.accounts()[1]["token"]) as neo:
            item = await neo.resolve_item_url(url, wait=False)
            retry_after = max(15, getattr(neo, "last_fetch_retry_after", 15))
        if item is None:
            current = dict(row.get("resolution") or {})
            current.update(
                {
                    "code": "fetch_pending",
                    "basis": "manual",
                    "retryable": True,
                    "pending_url": url,
                    "message": "NeoDB 正在抓取该外链，稍后会自动重试。",
                }
            )
            engine.db.update(
                engine.pid,
                sid,
                status=row["status"] if row["item"] else "resolve_failed",
                stage="resolve",
                error="NeoDB 正在抓取该外链，尚未返回可绑定的条目。",
                resolution=current,
            )
            return {
                "ok": True,
                "pending": True,
                "retry_after": retry_after,
                "message": current["message"],
            }
        engine.db.update(
            engine.pid,
            sid,
            item=item,
            status="pending",
            stage="",
            error="",
            plan=None,
            resolution={
                "code": "matched",
                "basis": "manual",
                "retryable": False,
                "message": "已由你确认对应条目。",
                "item": item,
                "candidates": (row.get("resolution") or {}).get("candidates", []),
            },
        )
        return {"ok": True}

    @app.get("/api/export/{kind}")
    async def export(kind: str):
        names = {
            "report": "migration-report.json",
            "failed": "failed.json",
            "source": "bangumi-export.json",
        }
        if kind not in names or not engine.pid:
            raise AppError("当前没有可导出的记录。")
        path = data / "profiles" / engine.pid / names[kind]
        if kind != "source":
            engine.report()
        if not path.exists():
            raise AppError("请先完成一次扫描。")
        return FileResponse(path, filename=names[kind], media_type="application/json")

    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    return app
