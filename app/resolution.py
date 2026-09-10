"""Candidate collection and explicit matching decisions; never writes collections."""

from urllib.parse import urlsplit

from app.models import suggested_neodb_types, suggested_search_category
from app.neodb import title_keys

MAX_QUERIES = 6
MAX_PAGES = 3


def source_titles(source, detail=None):
    subject = source.get("subject") or {}
    titles = [subject.get("name_cn"), subject.get("name")]
    if detail:
        titles.extend([detail.get("name_cn"), detail.get("name")])
        for field in detail.get("infobox") or []:
            if not isinstance(field, dict) or field.get("key") != "别名":
                continue
            value = field.get("value")
            if isinstance(value, str):
                titles.append(value)
            elif isinstance(value, list):
                titles.extend(v.get("v") for v in value if isinstance(v, dict))
    return list(dict.fromkeys(t.strip() for t in titles if isinstance(t, str) and t.strip()))


def decision(code, message, candidates=(), *, item=None, retryable=False, basis=None):
    return dict(
        code=code,
        message=message,
        candidates=list(candidates),
        item=item,
        retryable=retryable,
        basis=basis,
    )


def source_id(url):
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username
            or parsed.password
            or parsed.hostname not in {"bgm.tv", "bangumi.tv", "chii.in"}
            or parsed.port not in (None, 80, 443)
        ):
            return None
        parts = parsed.path.strip("/").split("/")
        return int(parts[1]) if len(parts) == 2 and parts[0] == "subject" else None
    except (ValueError, TypeError):
        return None


def choose(candidates, titles, types, sid, exclude=(), complete=True):
    candidates = list({item["uuid"]: item for item in candidates}.values())
    typed = [
        item for item in candidates if not types or (item.get("type") or "").casefold() in types
    ]
    linked = [
        item
        for item in typed
        if any(source_id(ref.get("url")) == sid for ref in item.get("external_resources", []))
    ]
    wanted = set().union(*(title_keys(title) for title in titles))
    exact = [
        item
        for item in typed
        if wanted
        & set().union(*(title_keys(title) for title in item.get("titles", [item["title"]])))
        and not any(
            source_id(ref.get("url")) not in (None, sid)
            for ref in item.get("external_resources", [])
        )
    ]
    matches = linked or exact
    if not complete:
        return decision(
            "incomplete",
            "搜索结果尚未完整返回，系统不会自动选择，请从候选中确认或重新查找。",
            candidates,
        )
    if len(matches) > 1:
        return decision("ambiguous", "存在多个匹配候选，请确认具体作品或版本。", candidates)
    if len(matches) == 1:
        if matches[0]["uuid"] in set(exclude):
            return decision("ambiguous", "请确认要使用的作品或版本。", candidates)
        return decision(
            "matched",
            "已确认对应作品。",
            candidates,
            item=matches[0],
            basis="source_url" if linked else "exact_title",
        )
    return decision(
        "no_match", "搜索未找到可确认的对应条目，可查看候选或补充作品来源链接。", candidates
    )


async def inspect_candidates(neo, source, detail=None, exclude=()):
    titles = source_titles(source, detail)
    queries = list(dict.fromkeys(" ".join(t.replace("-", " ").split()) for t in titles))
    queries = [q for q in queries if q]
    complete = len(queries) <= MAX_QUERIES
    candidates = {}
    subject_type = (source.get("subject") or {}).get("type") or source.get("subject_type")
    category = suggested_search_category(subject_type)
    # The captured OpenAPI explicitly supports the combined movie,tv category.
    for query in queries[:MAX_QUERIES]:
        for page in range(1, MAX_PAGES + 1):
            hits, pages, valid = await neo.search_page(query, category=category, page=page)
            complete = complete and valid
            candidates.update((hit["uuid"], hit) for hit in hits)
            if page >= pages:
                break
            if page == MAX_PAGES:
                complete = False
    result = choose(
        candidates.values(),
        titles,
        suggested_neodb_types(subject_type),
        source["subject_id"],
        exclude,
        complete,
    )
    result.update(titles=titles, complete=complete)
    return result
