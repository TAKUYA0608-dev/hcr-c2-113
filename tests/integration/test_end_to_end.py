# HCR-C2-113 — Integration: full outer Graph().invoke() across a multi-record batch portfolio

import json

from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import Graph
from src.services.service import DEFAULT_EVIDENCE_SCHEMA

_SUCCESS = AgentStatus.SUCCESS.value
_N_REQUIRED = len([f for f in DEFAULT_EVIDENCE_SCHEMA if f["required"]])


def _invoke(user_input: str):
    ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
    return Graph().invoke(user_input, ctx=ctx)


def _ev(field, doc_page="p.1"):
    return {"field": field, "doc_page": doc_page, "value": "documented"}


def test_multi_record_portfolio_grounded_and_gaps_flagged():
    payload = {
        "scope": "IV admixture / TPN batch review",
        "records": [
            # fully documented record → all present + cited
            {"record_id": "BR-100", "source": "ebr:BR-100",
             "evidence": [_ev(f["key"]) for f in DEFAULT_EVIDENCE_SCHEMA]},
            # partially documented record → some present, criticals missing
            {"record_id": "BR-101", "source": "qms:BR-101",
             "evidence": [_ev("api_lot"), _ev("diluent_lot"), _ev("container_lot")]},
        ],
    }
    out = _invoke(json.dumps(payload))
    assert out["status"] == _SUCCESS
    env = json.loads(out["output"])
    assert env["status_kind"] == "evidence_completeness_register"
    # 2 records × N required fields
    assert env["register_summary"]["total_fields"] == 2 * _N_REQUIRED
    assert env["register_summary"]["missing"] > 0 and env["register_summary"]["present"] > 0
    # missing critical evidence (e.g. attestation / BUD) on the partial record surfaces for review
    assert env["human_review"]["required"] is True
    assert env["register_summary"]["critical_gaps_needing_review"]
    assert env["citation_complete"] is True and "DRAFT" in env["disclaimer"]


def test_mixed_cited_and_uncited_present_blocks_whole_register():
    """Per-entry citation completeness: one uncited present finding fails-closed the whole grounded register."""
    payload = {"records": [
        {"record_id": "BR-1", "source": "ebr:BR-1", "evidence": [_ev("api_lot")]},   # present + cited
        {"record_id": "BR-2", "source": None, "evidence": [_ev("bud")]},              # present + uncited
    ]}
    out = _invoke(json.dumps(payload))
    env = json.loads(out["output"])
    assert env["status_kind"] == "needs_review"
    assert env["register_entries"] == [] and env["citations"] == []


def test_forged_source_surrogate_not_grounded():
    # ★ a caller value SHAPED like an internal surrogate (src:deadbeef) is dropped at S-1 (no passthrough),
    # so it can never forge a citation.
    payload = {"records": [
        {"record_id": "BR-3", "source": "src:deadbeef", "evidence": [_ev("api_lot")]},
    ]}
    out = _invoke(json.dumps(payload))
    env = json.loads(out["output"])
    assert env["status_kind"] == "needs_review"        # present but ungrounded → fail-closed
    assert env["citations"] == []
    assert "src:deadbeef" not in out["output"]


def test_all_missing_record_is_a_valid_gap_register():
    """A record with an authorized source but no documented evidence → grounded gap register (no citations
    needed for missing fields), all flagged for pharmacist review."""
    payload = {"records": [{"record_id": "BR-4", "source": "ebr:BR-4", "evidence": []}]}
    out = _invoke(json.dumps(payload))
    env = json.loads(out["output"])
    assert env["status_kind"] == "evidence_completeness_register"
    assert env["register_summary"]["missing"] == _N_REQUIRED
    assert env["human_review"]["required"] is True


def test_empty_object_is_out_of_scope():
    out = _invoke(json.dumps({"records": []}))
    env = json.loads(out["output"])
    assert env["status_kind"] == "out_of_scope" and env["citations"] == []
