"""HCR-C2-113 — deterministic domain services (no framework imports, no LLM).

BatchRecordEvidenceService: normalizes an already-completed sterile-compounding batch record into a
canonical evidence signal set, locates each required field of the seeded, approved in-house evidence schema
(attestation / lot / timing / source-doc link), determines presence/absence + a deterministic confidence
band, keeps a document/page provenance citation for every present field, and composes a candidate
**EvidenceCompletenessRegister**.

Everything here is deterministic and auditable (alias normalization + schema field composition + set
membership + keyed clause composition) — there is **no LLM** (no model in config/agent.yaml, no LLM
dependency in pyproject, no LLM call anywhere in src/). The "bounded mapping" of an unevenly-worded
attestation label to a controlled schema field is done deterministically via a curated alias index, never a
model. Records are keyed by an opaque, non-reversible ``record_ref`` surrogate; patient / staff PII and the
raw attestation *value* are never carried into the register (only presence is), and the S-3 output gate
re-redacts anything that leaks. The seeded evidence schema / provenance registry are overridable by CoE (a
change-controlled engineer MR + specialist review) without touching node logic.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

# Two SEPARATE concerns — do not conflate them:
#   (1) PRIVACY (opaque_id): every caller identifier (record_id) is UNCONDITIONALLY tokenized to a
#       deterministic, non-reversible opaque surrogate so PHI/PII (even a bare name like ``Yamada`` /
#       ``Taro.Yamada`` / ``TaroYamada``, no spaces/symbols) can never reach a citation or the register.
#       Tokenizing is a privacy measure — it does NOT assert the value is authorized/verifiable. Surrogates
#       are one-way hashes; graph state never stores a surrogate→raw rejoin map.
#   (2) PROVENANCE (resolve_provenance): a caller ``source`` becomes a grounded CITATION only when it is
#       resolvable against the authorized provenance registry (names a trusted clinical document system of
#       record). Any other free text (a patient/staff name, ``unknown``, a fabricated value, or a caller
#       value merely SHAPED like a surrogate ``src:1a2b3c4d``) is NOT verifiable provenance → it yields NO
#       citation → S-3 blocks a present finding as CITATION_INCOMPLETE (fail-closed). "Tokenized" is never
#       sufficient for a citation.
# Tokenization is UNCONDITIONAL (no syntactic passthrough): a caller value merely *shaped* like a surrogate
# (``rec:deadbeef``) is re-hashed, never trusted. Identifiers / provenance are resolved exactly once at S-1
# (pre_process); downstream trusts that resolution verbatim.
_SAFE_TOKEN = re.compile(r"^[a-z0-9_\-]{1,64}$")

# Authorized provenance registry: the clinical / quality document systems of record a deploying hospital
# pharmacy trusts as verifiable evidence sources. A caller ``source`` is accepted as a grounded citation ONLY
# when its leading namespace names one of these (the "trusted context"). This is the deploying org's / CoE's
# registry — overridable without touching node logic; it is a SEMANTIC allowlist of authorized systems, not a
# syntactic character class.
AUTHORIZED_PROVENANCE_SYSTEMS = frozenset(
    {
        "ebr",
        "mbr",
        "bpr",
        "cmr",
        "batch_record_system",
        "compounding_log",
        "compounding_record",
        "dms",
        "edms",
        "document_management",
        "record_system",
        "system_of_record",
        "sor",
        "emr",
        "emr_pharmacy",
        "his",
        "pharmacy_system",
        "hpm",
        "qms",
        "trackwise",
        "veeva",
        "lims",
        "mes",
        "sap",
        "authorized_feed",
        "evidence_feed",
    }
)


def _sha8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def opaque_id(value: Any, prefix: str) -> str:
    """PRIVACY tokenize a caller identifier to a deterministic, non-reversible opaque surrogate
    ``<prefix>:<sha8>``.

    Caller identifiers are **always** tokenized — no syntactic passthrough — so PHI/PII (with or without
    spaces) can never survive into a citation or the register, and a caller value merely *shaped* like a
    surrogate (``rec:deadbeef``) is re-hashed rather than trusted. Same input → same surrogate (register
    entries / citations / summary stay joinable within one invocation). This is a privacy measure only; it
    makes no claim that the identifier is authorized, and no surrogate→raw rejoin map is ever kept.
    """
    return f"{prefix}:{_sha8(str(value or '').strip())}"


def resolve_provenance(value: Any) -> str | None:
    """Resolve a **raw** caller ``source`` to a grounded, privacy-tokenized CITATION — or ``None``.

    Provenance validation (separate from privacy) and the **single** resolution point (S-1 / pre_process).
    A citation is emitted **only** when the source names an authorized clinical document system of record
    (``<authorized-namespace>[:<ref>]``). Any other value — a patient/staff name, ``unknown``, a fabricated
    value, **or a value that merely looks like a surrogate (``src:1a2b3c4d``)** — is not verifiable
    provenance and returns ``None`` so the S-3 gate blocks a present finding as CITATION_INCOMPLETE
    (fail-closed). When authorized, the raw label is never used verbatim: the citation is a privacy hash
    (``src:<sha8>``) of the authorized reference. No synthetic provenance is fabricated.

    ★ Forged-surrogate defence: there is **no format-based passthrough**. A caller-supplied ``src:<hex>``
    has namespace ``src`` (not an authorized system of record), so it resolves to ``None`` — it is dropped
    here at S-1 and can never reach a citation. Because provenance is resolved exactly once (here), the
    produced ``src:<sha8>`` is the trusted citation downstream and is **never** fed back through this
    function (which would, correctly, reject it), so no forged value can imitate an internal surrogate.
    """
    text = str(value or "").strip()
    if not text:
        return None
    namespace = text.split(":", 1)[0].strip().lower()
    if namespace not in AUTHORIZED_PROVENANCE_SYSTEMS:
        return None  # unverifiable / forged-surrogate provenance → fail-closed (no citation → needs_review)
    return "src:" + _sha8(text)


# ── seeded, approved in-house evidence schema (authorized clauses, CoE-calibratable per deployment) ──
# Each required field the sterile-compounding batch record must document. ``aliases`` capture the uneven
# wording seen across hand-written / abbreviated records so the (deterministic) locate step can map a
# record's evidence entry to the controlled field. ``clause_id@version`` is a stable, citable reference to
# the approved schema clause. This is the deploying pharmacy's / CoE's schema — overridable via a
# change-controlled engineer MR + specialist review, without touching node logic.
DEFAULT_EVIDENCE_SCHEMA: list[dict[str, Any]] = [
    {
        "key": "compounder_attestation",
        "label": "調製者 attestation / sign-off",
        "category": "attestation",
        "required": True,
        "critical": True,
        "clause_id": "ES-ATT-COMPOUNDER",
        "version": "v3",
        "aliases": ["compounder_signoff", "preparer_attestation", "prepared_by", "調製者確認", "調製者署名"],
    },
    {
        "key": "verifier_attestation",
        "label": "監査者 / second-check sign-off",
        "category": "attestation",
        "required": True,
        "critical": True,
        "clause_id": "ES-ATT-VERIFIER",
        "version": "v3",
        "aliases": ["verifier_signoff", "checker_attestation", "verified_by", "double_check", "監査者確認"],
    },
    {
        "key": "api_lot",
        "label": "原薬 (active ingredient) lot 番号",
        "category": "lot",
        "required": True,
        "critical": True,
        "clause_id": "ES-LOT-API",
        "version": "v3",
        "aliases": ["active_ingredient_lot", "drug_lot", "api_lot_number", "原薬lot", "主薬lot"],
    },
    {
        "key": "diluent_lot",
        "label": "溶解液 / 希釈液 lot",
        "category": "lot",
        "required": True,
        "critical": False,
        "clause_id": "ES-LOT-DILUENT",
        "version": "v3",
        "aliases": ["diluent_lot_number", "solvent_lot", "希釈液lot", "溶解液lot"],
    },
    {
        "key": "container_lot",
        "label": "容器 / 器材 lot",
        "category": "lot",
        "required": True,
        "critical": False,
        "clause_id": "ES-LOT-CONTAINER",
        "version": "v3",
        "aliases": ["container_lot_number", "device_lot", "bag_lot", "容器lot"],
    },
    {
        "key": "compounding_timing",
        "label": "調製開始 / 終了時刻",
        "category": "timing",
        "required": True,
        "critical": False,
        "clause_id": "ES-TIM-COMPOUND",
        "version": "v3",
        "aliases": ["compounding_start_time", "compounding_end_time", "prep_time", "調製時刻"],
    },
    {
        "key": "bud",
        "label": "使用期限 (beyond-use date)",
        "category": "timing",
        "required": True,
        "critical": True,
        "clause_id": "ES-TIM-BUD",
        "version": "v3",
        "aliases": ["beyond_use_date", "expiry", "bud_date", "使用期限"],
    },
    {
        "key": "aseptic_technique_ref",
        "label": "無菌操作手順 / 清掃記録 source-doc link",
        "category": "source_doc",
        "required": True,
        "critical": False,
        "clause_id": "ES-DOC-ASEPTIC",
        "version": "v3",
        "aliases": ["aseptic_sop_ref", "cleaning_record_ref", "sop_link", "無菌操作参照", "清掃記録参照"],
    },
    {
        "key": "env_monitoring_ref",
        "label": "環境モニタリング参照",
        "category": "source_doc",
        "required": True,
        "critical": False,
        "clause_id": "ES-DOC-ENVMON",
        "version": "v3",
        "aliases": ["environmental_monitoring_ref", "em_ref", "環境測定参照", "環境モニタリング"],
    },
]

# alias / canonical-key → canonical schema key (lower-cased); built once from the seeded schema.
_ALIAS_INDEX: dict[str, str] = {}
for _field in DEFAULT_EVIDENCE_SCHEMA:
    _ALIAS_INDEX[_field["key"].lower()] = _field["key"]
    for _alias in _field["aliases"]:
        _ALIAS_INDEX[str(_alias).strip().lower()] = _field["key"]

_SCHEMA_BY_KEY: dict[str, dict[str, Any]] = {f["key"]: f for f in DEFAULT_EVIDENCE_SCHEMA}

# Deterministic confidence bands for a completeness finding.
_CONF_HIGH = "high"  # present with a document/page anchor
_CONF_MED = "medium"  # present but no document/page anchor (ungrounded → needs pharmacist review)
_CONF_NONE = "none"  # missing


def _clean(value: Any) -> str:
    return str(value or "").strip()


class BatchRecordEvidenceService:
    """Deterministic evidence locate, completeness assessment, citation resolution, and register composition."""

    @staticmethod
    def schema() -> list[dict[str, Any]]:
        """The seeded, approved required-field schema (read-only view)."""
        return DEFAULT_EVIDENCE_SCHEMA

    # ── locate: anchor each required schema field to a record's evidence + provenance ────────
    @staticmethod
    def locate_record_fields(raw_record: dict[str, Any]) -> list[dict[str, Any]]:
        """Locate every required schema field for one batch record, anchored to the record's supplied
        evidence entry (if any) + resolved provenance. Deterministic.

        The raw patient/staff reference and the free-text attestation *value* are intentionally reduced to a
        presence flag + opaque IDs — they are never carried into the located field (only presence + the
        document/page anchor are). ``record_id`` is always privacy-tokenized. ``source`` was already resolved
        to a grounded citation (``src:<sha8>``) or ``None`` by pre_process (S-1), the single
        provenance-resolution point — a forged surrogate was dropped there. locate trusts that value verbatim;
        it never re-resolves and never fabricates provenance.
        """
        raw_id = _clean(raw_record.get("record_id") or raw_record.get("id"))
        if not raw_id:
            return []  # malformed — dropped (no record identifier), mirrors S-1 record minimisation
        record_ref = opaque_id(raw_id, "rec")  # tokenized unconditionally (no forgeable passthrough)
        source = raw_record.get("source")  # already resolved (src:<sha8> or None) at S-1

        # Map the record's supplied evidence entries → canonical schema key. Uneven / abbreviated field
        # labels are mapped via the curated alias index (deterministic bounded mapping, no LLM). Only
        # presence + the document/page anchor are retained; the attestation value is never carried onward.
        # Evidence arrives either already minimised by pre_process (``has_value`` flag, raw value dropped) or
        # as a raw ``value`` in a direct service call — both reduce to a presence flag only.
        evidence_by_key: dict[str, dict[str, Any]] = {}
        entries = raw_record.get("evidence")
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            raw_field = _clean(entry.get("field") or entry.get("field_key")).lower()
            canonical = _ALIAS_INDEX.get(raw_field)
            if canonical is None:
                continue  # not a recognized schema field → ignored (never echoed to output)
            has_value = bool(entry.get("has_value")) or bool(_clean(entry.get("value")))
            doc_page = _safe_doc_page(entry.get("doc_page"))
            # keep the first evidence entry per field (idempotent); a later duplicate does not weaken it
            prior = evidence_by_key.get(canonical)
            if prior is None or (not prior["has_value"] and has_value) or (not prior["doc_page"] and doc_page):
                evidence_by_key[canonical] = {"has_value": has_value, "doc_page": doc_page}

        located: list[dict[str, Any]] = []
        for field in DEFAULT_EVIDENCE_SCHEMA:
            key = field["key"]
            found = evidence_by_key.get(key, {"has_value": False, "doc_page": None})
            item_id = opaque_id(f"{record_ref}|{key}", "evi")  # stable per-(record,field) opaque join key
            located.append(
                {
                    "item_id": item_id,
                    "record_ref": record_ref,
                    "field_key": key,
                    "field_label": field["label"],
                    "category": field["category"],
                    "required": field["required"],
                    "critical": field["critical"],
                    "clause": f"{field['clause_id']}@{field['version']}",
                    "has_value": found["has_value"],
                    "doc_page": found["doc_page"],
                    "source": source,
                }
            )
        return located

    # ── assess: present / missing + deterministic confidence band ────────────────────────────
    @staticmethod
    def assess_completeness(item: dict[str, Any]) -> dict[str, Any]:
        """Determine presence/absence + a deterministic confidence band for one located field.

        Deterministic threshold logic only — the free-text attestation value is never interpreted
        semantically (only its presence is used), so prompt-like text in a supplied field can never influence
        the assessment.
        """
        has_value = bool(item.get("has_value"))
        has_anchor = bool(item.get("doc_page"))
        if has_value and has_anchor:
            status, confidence = "present", _CONF_HIGH
        elif has_value:
            status, confidence = "present", _CONF_MED  # asserted present but ungrounded (no page anchor)
        else:
            status, confidence = "missing", _CONF_NONE
        out = dict(item)
        out.update(
            {
                "status": status,
                "confidence": confidence,
                "missing_data": status == "missing",
            }
        )
        return out

    # ── citation: fail-closed provenance for present findings + review flag ───────────────────
    @staticmethod
    def check_citation(finding: dict[str, Any]) -> dict[str, Any]:
        """Resolve the citation for one assessed finding and set the pharmacist-review flag (deterministic).

        A present finding is cited **only** when it has both a document/page anchor AND a verifiable
        provenance source (``src:<sha8>``, resolved once at S-1). A present finding lacking either is
        ungrounded → ``citation=None`` → S-3 fails it closed. A missing finding needs no citation (it is the
        evidence gap the register exists to surface) but a missing *critical* field always needs pharmacist
        review, as does any low-confidence / ungrounded present finding.
        """
        status = finding.get("status")
        source = finding.get("source")
        doc_page = finding.get("doc_page")
        citation = source if (status == "present" and doc_page and source) else None

        needs_review = (
            (status == "present" and citation is None)  # ungrounded present assertion
            or (status == "missing" and bool(finding.get("critical")))  # critical evidence gap
            or (finding.get("confidence") == _CONF_MED)  # low-confidence present finding
        )
        out = dict(finding)
        out.update({"citation": citation, "needs_pharmacist_review": bool(needs_review)})
        return out

    # ── compose: per-field register entry (candidate, cited where grounded) ───────────────────
    @staticmethod
    def compose_entry(finding: dict[str, Any]) -> dict[str, Any]:
        """Compose the per-field candidate register entry (needs-review, whitelist-by-construction).

        The entry carries ONLY controlled, non-PII fields — never the raw attestation value or any arbitrary
        caller field. The entry is a candidate only; the final judgment (remediation, sterility, release,
        administration) defers to an authorized human pharmacist.
        """
        return {
            "item_id": finding["item_id"],
            "record_ref": finding["record_ref"],
            "field_key": finding["field_key"],
            "field_label": finding["field_label"],
            "category": finding["category"],
            "critical": finding["critical"],
            "status": finding["status"],
            "missing_data": finding["missing_data"],
            "confidence": finding["confidence"],
            "doc_page": finding.get("doc_page"),
            "clause": finding["clause"],
            "needs_pharmacist_review": finding["needs_pharmacist_review"],
            "status_kind": "needs_review",
            "note": "Candidate evidence-completeness finding only — this agent does not assure sterility, "
            "release the preparation, change the record, or make a clinical decision; the final "
            "judgment is an authorized human pharmacist / quality lead's.",
            "citation": finding.get("citation"),
        }

    @staticmethod
    def register_summary(entries: list[dict[str, Any]]) -> dict[str, Any]:
        """Portfolio-level rollup: entry count, status distribution, category distribution, gaps needing
        urgent (critical) review."""
        status_distribution: dict[str, int] = {}
        category_distribution: dict[str, int] = {}
        for e in entries:
            status_distribution[e["status"]] = status_distribution.get(e["status"], 0) + 1
            category_distribution[e["category"]] = category_distribution.get(e["category"], 0) + 1
        critical_gaps = [e["item_id"] for e in entries if e["status"] == "missing" and e["critical"]]
        return {
            "total_fields": len(entries),
            "present": status_distribution.get("present", 0),
            "missing": status_distribution.get("missing", 0),
            "status_distribution": status_distribution,
            "category_distribution": category_distribution,
            "critical_gaps_needing_review": critical_gaps,
        }


# ── document/page anchor: bounded location reference (never free-text PHI) ────────────────────
_DOC_PAGE_ALLOWED = re.compile(r"[^0-9A-Za-z_\-.,:/#§()＃．・（）　぀-ヿ一-鿿\s]")
_DOC_PAGE_MAX = 80


def _safe_doc_page(value: Any) -> str | None:
    """Constrain a caller document/page anchor to a bounded location reference (page/section only).

    The anchor is the one caller free-text that reaches the output (as the citation location), so it is
    constrained to a location charset (digits / latin / kana-kanji / page-section punctuation), stripped of
    anything else, and length-capped. The S-3 output gate re-redacts secrets / contact info as
    defense-in-depth."""
    text = _clean(value)
    if not text:
        return None
    text = _DOC_PAGE_ALLOWED.sub("", text).strip()
    return text[:_DOC_PAGE_MAX] or None
