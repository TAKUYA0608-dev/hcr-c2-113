"""HCR-C2-113 — pre_process node: BatchRecordIntake + SensitiveDataDetectAndMinimise (S-1 + S-2, pre-LLM).

Accepts a structured JSON request (a ``records[]`` array of already-completed sterile-compounding batch
records, plus optional ``scope``) or NL text, normalizes it (NFKC), enforces S-1/S-2, and extracts the
analysis slots. The agent is read-only: it never mutates the source record, asserts sterility, releases a
preparation, or makes a clinical decision.

Degraded contract (SDK 1.0.0): injection markers / oversize / empty never set ``status=ERROR``. They return
``status=SUCCESS + error_code`` (``INJECTION_REJECTED`` / ``INPUT_TOO_LONG`` / ``INPUT_REJECTED``) and
**discard the offending body** so ``main``/``post_process`` still run (disclaimer + S-3 + S-4). The
``@final`` framework hook is not invoked by the local stub framework, so ``execute()`` re-checks the same S-2
conditions itself. Injection containment is pre-LLM: prompt-like input degrades to a safe out-of-scope
answer *without any semantic execution* of the offending text (and this template runs no LLM at all).

Field-level input hygiene (S-1) + sensitive-data minimisation (S-2, pre-LLM, PHI/PII) — the batch record's
patient and staff identifiers must never enter State:
- the record is projected to a strict allowlist ``{record_id, source, evidence[]}`` — every other caller
  field (patient name / patient ID / MRN / diagnosis / DOB / product / ward / free-text note …) is DROPPED,
  never masked (whitelist-by-construction at the record level);
- ``record_id`` is UNCONDITIONALLY tokenized to an opaque, non-reversible surrogate (a bare name is opaque
  like any value), with no surrogate→raw rejoin map kept in state;
- each ``evidence`` entry is reduced to ``{field, doc_page, has_value}`` — the raw attestation *value* (which
  may carry a staff name / signature) is DROPPED to a presence flag, so only *whether* a field is documented
  (never its content) is retained; ``doc_page`` is constrained to a bounded location reference;
- caller-supplied provenance (``source``) is constrained to a grounded citation only if it resolves to an
  authorized clinical document system of record; anything else is dropped so untrusted text can never reach a
  citation. ``scope`` free text is hygiened (credential / My-Number / email / phone redaction).
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.services.service import _safe_doc_page, opaque_id, resolve_provenance
from src.utils.audit import emit_trace_event

_MAX_INPUT = 300_000  # batch-record payloads can carry many evidence entries → larger cap than a chat prompt
_INJECTION_MARKERS = (
    "ignore previous",
    "ignore all previous",
    "disregard the above",
    "system prompt",
    "you are now",
    "###system",
    "<|im_start|>",
)
_REJECT_CODES = frozenset({"INJECTION_REJECTED", "INPUT_TOO_LONG"})
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# ── input hygiene: redact secrets a caller may inadvertently include before persisting to State ──
_CREDENTIAL = re.compile(r"\b(sk-[A-Za-z0-9]{8,}|AKIA[0-9A-Z]{12,}|eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,})\b")
_MY_NUMBER = re.compile(r"\b\d{12}\b")  # Japanese My-Number / 個人番号
_EMAIL = re.compile(r"\b[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63}){1,4}\b")
_PHONE = re.compile(r"(?<![\d.])(?:(?:\+81[-\s]?\d{1,4}|0\d{1,4})[-\s]?\d{1,4}[-\s]?\d{3,4})(?![\d.])")
_REDACTED = "[REDACTED]"


def _nfkc(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "")


def _hygiene(text: str) -> str:
    """Redact credential / My-Number / email / phone patterns from a free-text value."""
    out = _CREDENTIAL.sub(_REDACTED, text)
    out = _MY_NUMBER.sub(_REDACTED, out)
    out = _EMAIL.sub(_REDACTED, out)
    out = _PHONE.sub(_REDACTED, out)
    return out


def _minimise_evidence(entries: Any) -> list[dict[str, Any]]:
    """Reduce each evidence entry to ``{field, doc_page, has_value}`` — the raw value is dropped to a
    presence flag so no attestation content (a staff name / signature) is ever carried into State."""
    out: list[dict[str, Any]] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        field = _hygiene(str(entry.get("field") or entry.get("field_key") or "").strip().lower())
        if not field:
            continue
        out.append(
            {
                "field": field,
                "doc_page": _safe_doc_page(entry.get("doc_page")),
                "has_value": bool(str(entry.get("value") or "").strip()),  # presence only — value dropped
            }
        )
    return out


def _minimise_record(raw: Any) -> dict[str, Any] | None:
    """Project one batch record to the strict allowlist ``{record_id, source, evidence[]}``.

    Every other caller field (patient name / MRN / diagnosis / product / note …) is dropped. Records without a
    ``record_id`` are dropped as malformed."""
    if not isinstance(raw, dict):
        return None
    raw_id = str(raw.get("record_id") or raw.get("id") or "").strip()
    if not raw_id:
        return None
    return {
        "record_id": opaque_id(raw_id, "rec"),  # tokenized unconditionally (no passthrough)
        "source": resolve_provenance(raw.get("source")),  # grounded citation or None (single resolution point)
        "evidence": _minimise_evidence(raw.get("evidence")),
    }


class PreProcessNode(FunctionNode):
    """Validate + minimise the batch-record request and extract its records / scope slots."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _reject_code(self, raw: str) -> str | None:
        if len(raw) > _MAX_INPUT:
            return "INPUT_TOO_LONG"
        if any(marker in _nfkc(raw).lower() for marker in _INJECTION_MARKERS):
            return "INJECTION_REJECTED"
        return None

    def _extra_security_gate_input(self, state: dict[str, Any]) -> dict[str, Any]:
        """S-2 domain checks: size cap + prompt-injection markers (pre-LLM containment).

        SDK 1.0.0 contract: MUST NOT raise, and MUST NOT set status=ERROR (that would short-circuit the
        pipeline past post_process). A rejection is surfaced as a degraded ``SUCCESS + error_code``; the
        offending body is discarded by execute() with no semantic execution of the prompt-like text.
        """
        raw = str(state.get("user_input") or "")  # coerce non-string caller input (S-2 never raises)
        code = self._reject_code(raw)
        if code:
            out = dict(state)
            out["error_code"] = code
            return out
        return dict(state)

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        raw = str(state.get("user_input") or "")  # coerce non-string caller input (S-2 never raises)
        input_context = state.get("input_context", {})  # read-only caller context [C1]
        enriched = json.dumps(
            {
                "source": "HospitalSterileCompoundingBatchRecordEvidenceCompletenessAgent",
                "channel": input_context.get("channel", "unknown"),
            },
            ensure_ascii=False,
        )

        # Degrade on rejection: the S-2 hook may already have set error_code (real SDK); re-detect here
        # because the local stub framework does not invoke the hook. Discard the offending body entirely.
        prior = state.get("error_code")
        code = prior if prior in _REJECT_CODES else self._reject_code(raw)
        if code:
            emit_trace_event("batch_record_intake.rejected", {"reason": code}, state)
            return {
                "validated_input": "{}",
                "input_format": "rejected",
                "enriched_context": enriched,
                "user_input": "",
                "error_code": code,
                "status": AgentStatus.SUCCESS.value,
            }

        if not raw.strip():
            emit_trace_event("batch_record_intake.rejected", {"reason": "empty_input"}, state)
            return {
                "validated_input": "{}",
                "input_format": "empty",
                "enriched_context": enriched,
                "error_code": "INPUT_REJECTED",
                "status": AgentStatus.SUCCESS.value,
            }

        slots, fmt = self._parse(_CONTROL.sub("", _nfkc(raw)))
        emit_trace_event(
            "batch_record_intake.validated",
            {"input_format": fmt, "record_count": len(slots["records"]), "scope": slots.get("scope")},
            state,
        )
        return {
            "validated_input": json.dumps(slots, ensure_ascii=False),
            "input_format": fmt,
            "enriched_context": enriched,
            "status": AgentStatus.SUCCESS.value,
        }

    def _parse(self, text: str) -> tuple[dict[str, Any], str]:
        try:
            obj = json.loads(text)
        except (ValueError, TypeError):
            return {"records": [], "scope": None}, "text"
        if isinstance(obj, dict):
            records = obj.get("records")
            records = records if isinstance(records, list) else []
            minimised = [r for r in (_minimise_record(x) for x in records) if r is not None]
            scope = obj.get("scope")
            return {"records": minimised, "scope": _hygiene(str(scope).strip()) if scope else None}, "json"
        if isinstance(obj, list):  # bare records array
            minimised = [r for r in (_minimise_record(x) for x in obj) if r is not None]
            return {"records": minimised, "scope": None}, "json"
        return {"records": [], "scope": None}, "text"
