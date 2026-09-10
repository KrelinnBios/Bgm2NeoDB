import json
from pathlib import Path

import yaml

from app.models import NEO_SEARCH_CATEGORY_BY_BANGUMI, plan_collection

DOCS = Path(__file__).parents[1] / "docs"


def test_search_categories_match_captured_official_schema():
    schema = json.loads((DOCS / "neodb-openapi.json").read_text())
    supported = schema["components"]["schemas"]["SearchableItemCategory"]["enum"]
    assert set(NEO_SEARCH_CATEGORY_BY_BANGUMI.values()) <= set(supported)


def test_payload_fields_match_captured_official_schema(source):
    schema = json.loads((DOCS / "neodb-openapi.json").read_text())
    inputs = schema["components"]["schemas"]["MarkInSchema"]
    payload = plan_collection(source, None)["payload"]
    assert set(payload) <= set(inputs["properties"])
    assert set(inputs["required"]) <= set(payload)
    assert inputs["properties"]["rating_grade"]["maximum"] == 10


def test_bangumi_export_fields_and_paging_match_official_schema():
    schema = yaml.safe_load((DOCS / "bangumi-v0.yaml").read_text(encoding="utf-8"))
    props = schema["components"]["schemas"]["UserSubjectCollection"]["properties"]
    assert {
        "subject_id",
        "subject_type",
        "type",
        "rate",
        "comment",
        "tags",
        "ep_status",
        "vol_status",
        "private",
        "updated_at",
    } <= props.keys()
    assert schema["components"]["parameters"]["default_query_limit"]["schema"]["maximum"] == 50
