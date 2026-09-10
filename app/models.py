from copy import deepcopy

from app.errors import AppError

STATUS_MAP = {1: "wishlist", 2: "complete", 3: "progress", 4: "progress", 5: "dropped"}
STATUS_LABELS = {1: "想看", 2: "看过", 3: "在看", 4: "搁置", 5: "抛弃"}
MARK_FIELDS = ("shelf_type", "visibility", "rating_grade", "comment_text", "tags")


def bangumi_url(subject_id):
    if type(subject_id) is not int or subject_id <= 0:
        raise AppError("Bangumi 条目编号无效。")
    return f"https://bgm.tv/subject/{subject_id}"


def normalized_title(text):
    if not isinstance(text, str):
        return ""
    return "".join(text.split()).casefold()


# 当 bgm.tv 无法抓取（例如成人条目对匿名访问隐藏）时，用精确标题匹配
# 在 NeoDB 中查找已有条目；NeoDB 条目类型以大小写不敏感方式比较。
# 类型名取自实例实际返回值（Edition/TVSeason/…），不要写 NeoDB 从不返回的
# 名字：曾用 "book" 表示图书，导致同名图书全部被类型过滤丢弃。
NEO_TYPES_BY_BANGUMI = {
    1: {"edition", "work"},
    2: {"tvseason", "tvshow", "tvspecial", "movie", "ova", "special", "show", "tv"},
    3: {"album", "podcast"},
    4: {"game"},
    6: {
        "movie",
        "tvseason",
        "tvshow",
        "tvspecial",
        "performance",
        "performanceproduction",
        "show",
        "tv",
        "video",
    },
}


def suggested_neodb_types(subject_type):
    return set(NEO_TYPES_BY_BANGUMI.get(subject_type, ()))


NEO_SEARCH_CATEGORY_BY_BANGUMI = {
    1: "book",
    2: "movie,tv",
    3: "music",
    4: "game",
}


def suggested_search_category(subject_type):
    return NEO_SEARCH_CATEGORY_BY_BANGUMI.get(subject_type)


def clean_tags(tags):
    if not isinstance(tags, list) or any(not isinstance(t, str) for t in tags):
        raise AppError("标签数据格式无效。")
    return list(dict.fromkeys(t.strip() for t in tags if t.strip()))


def map_rating(collection_type, rate):
    if type(rate) is not int or not 0 <= rate <= 10:
        raise AppError("Bangumi 评分超出 0–10 的范围。")
    return None if collection_type == 4 or rate == 0 else rate


def normalized_mark(mark):
    if mark is None:
        return None
    if any(k not in mark for k in MARK_FIELDS):
        raise AppError("NeoDB 收藏缺少必要字段，已停止以保护现有数据。")
    if (
        mark["shelf_type"] not in STATUS_MAP.values()
        or type(mark["visibility"]) is not int
        or mark["visibility"] not in (0, 1, 2)
    ):
        raise AppError("NeoDB 收藏状态或可见性无法确认。")
    rating = mark["rating_grade"] or 0
    if type(rating) is not int or not 0 <= rating <= 10:
        raise AppError("NeoDB 评分格式无法确认。")
    comment = mark["comment_text"] or ""
    if not isinstance(comment, str):
        raise AppError("NeoDB 短评格式无法确认。")
    return {
        "shelf_type": mark["shelf_type"],
        "visibility": mark["visibility"],
        "rating_grade": rating,
        "comment_text": comment,
        "tags": sorted(clean_tags(mark["tags"])),
    }


def plan_collection(source, current, import_date=True):
    status = source.get("type")
    if type(status) is not int or status not in STATUS_MAP:
        raise AppError("未知 Bangumi 收藏状态，无法迁移。")
    if type(source.get("private")) is not bool:
        raise AppError("无法确认 Bangumi 收藏的隐私设置，已阻止迁移。")
    before = normalized_mark(current)
    rating = map_rating(status, source.get("rate"))
    comment = source.get("comment") or ""
    if not isinstance(comment, str):
        raise AppError("Bangumi 短评格式无效。")
    tags = clean_tags(source.get("tags", []))
    # The shelf endpoint replaces these fields even when omitted. Preserve skipped fields.
    payload = (
        deepcopy(before)
        if before
        else {
            "rating_grade": 0,
            "comment_text": "",
            "tags": [],
            "visibility": 0,
        }
    )
    payload["shelf_type"] = STATUS_MAP[status]
    payload["visibility"] = max(payload["visibility"], 2 if source["private"] else 0)
    if rating is not None:
        payload["rating_grade"] = rating
    if comment:
        payload["comment_text"] = comment
    # Keep target-only tags; migration must not destroy existing user data.
    payload["tags"] = clean_tags(payload["tags"] + tags)
    payload["post_to_fediverse"] = False
    if current and current.get("created_time"):
        payload["created_time"] = current["created_time"]
    elif import_date and source.get("updated_at"):
        # 使用 Bangumi 的 updated_at 作为收藏日期（虽然不完全准确，但总比没有好）
        payload["created_time"] = source["updated_at"]
    after = normalized_mark(payload)
    diff = {
        k: {"before": before[k] if before else None, "after": after[k]}
        for k in MARK_FIELDS
        if before is None or before[k] != after[k]
    }
    notes = []
    if status == 4:
        notes.append("搁置转为在看，不导入 Bangumi 评分；已有 NeoDB 评分保留。")
    elif rating is None:
        notes.append("Bangumi 未评分，保留 NeoDB 已有评分。")
    if source.get("ep_status") or source.get("vol_status"):
        notes.append("进度仅保存到本地：话数、卷数与 NeoDB 条目的对应关系尚不能可靠确认。")
    if not current:
        if import_date:
            notes.append(
                "收藏日期使用 Bangumi 的最后修改时间（updated_at），可能不是首次收藏时间。"
            )
        else:
            notes.append("收藏日期不导入，NeoDB 将使用迁移时的当前时间。")
    return {
        "before": before,
        "after": after,
        "payload": payload,
        "diff": diff,
        "action": "create" if before is None else "update",
        "notes": notes,
        "progress_skipped": bool(source.get("ep_status") or source.get("vol_status")),
    }
