import pytest

from app.errors import AppError
from app.models import (
    bangumi_url,
    clean_tags,
    map_rating,
    normalized_mark,
    plan_collection,
    suggested_neodb_types,
    suggested_search_category,
)
from tests.conftest import mark


@pytest.mark.parametrize(
    "subject_type,expected",
    # NeoDB 实际返回的类型名；曾把图书写成不存在的 "book"，
    # 导致同名图书全部被类型过滤丢弃。
    [(1, "edition"), (2, "tvseason"), (3, "album"), (4, "game"), (6, "performance")],
)
def test_suggested_types_use_names_neodb_actually_returns(subject_type, expected):
    assert expected in suggested_neodb_types(subject_type)


def test_book_types_never_claim_nonexistent_book_type():
    assert "book" not in suggested_neodb_types(1)


def test_search_category_uses_neodb_catalog_values():
    assert suggested_search_category(1) == "book"
    assert suggested_search_category(4) == "game"
    assert suggested_search_category(6) is None


@pytest.mark.parametrize(
    "state,shelf",
    [(1, "wishlist"), (2, "complete"), (3, "progress"), (4, "progress"), (5, "dropped")],
)
def test_status_mapping(source, state, shelf):
    source["type"] = state
    assert plan_collection(source, None)["payload"]["shelf_type"] == shelf


@pytest.mark.parametrize("rate", range(11))
def test_on_hold_never_imports_rating(rate):
    assert map_rating(4, rate) is None


@pytest.mark.parametrize("rate", range(1, 11))
def test_valid_ratings_unchanged(rate):
    assert map_rating(2, rate) == rate


def test_empty_rating_and_comment_preserve_target(source):
    source.update(rate=0, comment="", tags=[])
    plan = plan_collection(source, mark(rating_grade=9, comment_text="已有短评", tags=["原标签"]))
    assert plan["payload"]["rating_grade"] == 9
    assert plan["payload"]["comment_text"] == "已有短评"
    assert plan["payload"]["tags"] == ["原标签"]
    assert plan["payload"]["created_time"] == "2021-01-01T00:00:00Z"


def test_on_hold_preserves_existing_rating(source):
    source["type"] = 4
    assert plan_collection(source, mark(rating_grade=9))["payload"]["rating_grade"] == 9
    assert plan_collection(source, None)["payload"]["rating_grade"] == 0


def test_tags_deduplicated_without_renaming(source):
    assert clean_tags([" a ", "a", "A", " ", "日本动画"]) == ["a", "A", "日本动画"]
    assert plan_collection(source, mark(tags=["既有标签"]))["payload"]["tags"] == [
        "既有标签",
        "动画",
    ]


def test_private_and_more_restrictive_target_protected(source):
    source["private"] = True
    assert plan_collection(source, None)["payload"]["visibility"] == 2
    source["private"] = False
    assert plan_collection(source, mark(visibility=2))["payload"]["visibility"] == 2
    assert plan_collection(source, mark(visibility=1))["payload"]["visibility"] == 1


def test_missing_privacy_fails_closed(source):
    del source["private"]
    with pytest.raises(AppError):
        plan_collection(source, None)


def test_idempotent_diff(source):
    first = plan_collection(source, None)
    second = plan_collection(source, first["payload"])
    assert second["diff"] == {}
    assert second["action"] == "update"


def test_progress_and_date_preserved_only_in_archive(source):
    plan = plan_collection(source, None, import_date=False)
    assert plan["progress_skipped"]
    assert "created_time" not in plan["payload"]
    assert "progress" not in plan["payload"]
    assert not plan["payload"]["post_to_fediverse"]


def test_import_date_uses_updated_at(source):
    plan = plan_collection(source, None, import_date=True)
    assert plan["payload"]["created_time"] == source["updated_at"]


def test_unknown_data_not_guessed(source):
    source["type"] = 99
    with pytest.raises(AppError):
        plan_collection(source, None)
    with pytest.raises(AppError):
        normalized_mark({"shelf_type": "complete"})
    with pytest.raises(AppError):
        map_rating(2, 11)


def test_subject_url_is_exact():
    assert bangumi_url(253) == "https://bgm.tv/subject/253"
    with pytest.raises(AppError):
        bangumi_url("253/evil")
