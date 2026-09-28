"""HCR-C2-113 — inner workflow step 1 (SoT Step 3): schema_field_locate.

Deterministic ingest of the supplied already-completed batch records, then, for each record, locates every
required field of the seeded approved in-house evidence schema (attestation / lot / timing / source-doc
link), anchoring each located field to the record's supplied evidence entry (document/page provenance) via a
curated alias index. Sets ``located_count`` (= records × required schema fields). **0 valid records (rejected
input, non-JSON text, or all rows missing record_id) routes to the out-of-scope safe answer** — the agent
never fabricates a finding for data it did not receive. The free-text content is never interpreted
semantically, so prompt-like text in a supplied field cannot influence the location.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.services.service import BatchRecordEvidenceService
from src.utils.audit import emit_trace_event


class SchemaFieldLocateNode(FunctionNode):
    """Locate every required schema field per record + anchor it to the record's evidence/provenance."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        # Records arrive already validated + minimised + provenance-resolved by pre_process (S-1): each
        # `source` is a grounded citation `src:<sha8>` or None (a forged surrogate was dropped at S-1). We do
        # not re-run provenance here — locate trusts that single upstream resolution.
        slots = json.loads(state.get("validated_input") or state.get("user_input") or "{}")
        if not isinstance(slots, dict):
            slots = {}
        canonical = json.dumps(slots, ensure_ascii=False)
        records = slots.get("records") if isinstance(slots.get("records"), list) else []

        if state.get("error_code") or not records:
            emit_trace_event("schema_field_locate.skip", {"reason": state.get("error_code") or "no_records"}, state)
            return {
                "validated_input": canonical,
                "located_fields": "[]",
                "located_count": 0,
                "error_code": state.get("error_code") or "NO_SCHEMA_FIELDS",
                "status": AgentStatus.SUCCESS.value,
            }

        located: list[dict[str, Any]] = []
        for record in records:
            if isinstance(record, dict):
                located.extend(BatchRecordEvidenceService.locate_record_fields(record))

        if not located:
            emit_trace_event("schema_field_locate.skip", {"reason": "all_malformed"}, state)
            return {
                "validated_input": canonical,
                "located_fields": "[]",
                "located_count": 0,
                "error_code": "NO_SCHEMA_FIELDS",
                "status": AgentStatus.SUCCESS.value,
            }

        category_distribution: dict[str, int] = {}
        for item in located:
            category_distribution[item["category"]] = category_distribution.get(item["category"], 0) + 1
        emit_trace_event(
            "schema_field_locate.complete",
            {"records": len(records), "located_fields": len(located), "category_distribution": category_distribution},
            state,
        )
        return {
            "validated_input": canonical,
            "located_fields": json.dumps(located, ensure_ascii=False),
            "located_count": len(located),
            "status": AgentStatus.SUCCESS.value,
        }
