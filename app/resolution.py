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
    """从候选列表中选择最佳匹配。

    优先级：
    1. 有 Bangumi 外链的（linked）- 即使搜索不完整也可以匹配
    2. 标题完全匹配的（exact）- 需要搜索完整

    改进：有外链匹配时忽略完整性检查，解决部分"重启后成功"的问题
    """
    candidates = list({item["uuid"]: item for item in candidates}.values())
    typed = [
        item for item in candidates if not types or (item.get("type") or "").casefold() in types
    ]
    linked = [
        item
        for item in typed
        if any(source_id(ref.get("url")) == sid for ref in item.get("external_resources", []))
    ]

    # 有外链匹配时，即使搜索不完整也可以返回
    if len(linked) == 1:
        if linked[0]["uuid"] in set(exclude):
            return decision("ambiguous", "请确认要使用的作品或版本。", candidates)
        return decision(
            "matched",
            "已确认对应作品。",
            candidates,
            item=linked[0],
            basis="source_url",
        )

    if len(linked) > 1:
        return decision("ambiguous", "存在多个匹配候选，请确认具体作品或版本。", candidates)

    # 标题匹配需要搜索完整
    if not complete:
        return decision(
            "incomplete",
            "搜索结果尚未完整返回，系统不会自动选择，请从候选中确认或重新查找。",
            candidates,
            retryable=True,
        )

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

    if len(exact) > 1:
        return decision("ambiguous", "存在多个匹配候选，请确认具体作品或版本。", candidates)

    if len(exact) == 1:
        if exact[0]["uuid"] in set(exclude):
            return decision("ambiguous", "请确认要使用的作品或版本。", candidates)
        return decision(
            "matched",
            "已确认对应作品。",
            candidates,
            item=exact[0],
            basis="exact_title",
        )

    return decision(
        "no_match", "搜索未找到可确认的对应条目，可查看候选或补充作品来源链接。", candidates
    )


def generate_search_queries(titles):
    """从标题列表生成多个搜索查询词。

    处理常见的标题格式：
    - 替换标点符号为空格
    - 生成带标点和不带标点的版本
    - 去重
    """
    queries = []
    for title in titles:
        if not title:
            continue

        # 原有逻辑：替换连字符并规范化空格
        normalized = " ".join(title.replace("-", " ").split())
        if normalized:
            queries.append(normalized)

        # 新增：处理更多标点符号（冒号、斜杠、点等）
        # 这些标点可能影响搜索结果
        for punct in [":", "：", "/", "／", "·", "・", "~", "～"]:
            if punct in title:
                variant = " ".join(title.replace(punct, " ").split())
                if variant and variant != normalized:
                    queries.append(variant)

    # 去重，保持顺序
    return list(dict.fromkeys(q for q in queries if q))


async def inspect_candidates(neo, source, detail=None, exclude=()):
    titles = source_titles(source, detail)
    queries = generate_search_queries(titles)
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
