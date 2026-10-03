# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Pure Work research citation review/rendering, without IO or semantic authority.

Sources are caller-supplied read results, not authenticated retrieval records.
A structurally valid result never establishes that a claim is true, that a read
occurred, or that a caller's complete-source declaration is accurate.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
from typing import Any

_TOOL_NAME = "review_research_report"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./:@%+~-]{0,511}\Z")
_SECTIONS = ("Scope", "Findings", "Limitations")
_KINDS = ("fact", "inference", "unknown", "omission")


def _plain(value: str) -> str:
    """Render supplied prose as one plain paragraph, never markup or a command."""
    value = " ".join(value.split())
    value = html.escape(value, quote=False)
    return re.sub(r"([\\`*_\[\]#])", r"\\\1", value)


def _multiple_sentences(text: str) -> bool:
    # Fail with actionable feedback instead of silently attaching one trailing
    # citation to independent sentences. Common within-sentence abbreviations
    # are not treated as sentence boundaries; this is not semantic parsing.
    for boundary in re.finditer(r'[.!?](?:["\'”’)]*)\s+(?=\S)|[。！？](?=\s*\S)', text):
        if boundary[0].startswith(".") and re.search(
            r"\b(?:vs|e\.g|i\.e|Mr|Mrs|Dr|Prof)$", text[:boundary.start()], re.I,
        ):
            continue
        return True
    return False


def review_research_report(sources, claims, question=None) -> dict[str, Any]:
    """Check supplied spans/quotes and attach adjacent references deterministically."""
    issues: list[dict[str, Any]] = []
    result = {
        "structural_valid": False,
        "rendered_markdown": None,
        "issues": issues,
        "input_fingerprint": None,
    }

    def issue(code, message, *, claim_id=None, source_id=None):
        issues.append({
            "code": code, "claim_id": claim_id, "source_id": source_id,
            "message": message,
        })

    if not isinstance(sources, list) or not isinstance(claims, list):
        issue("INVALID_INPUT", "sources and claims must be arrays")
        return result
    if not 1 <= len(sources) <= 32 or not 1 <= len(claims) <= 64:
        issue("LIMIT_EXCEEDED", "Use 1-32 sources and 1-64 claims")
        return result
    if question is not None and (not isinstance(question, str) or not question.strip()):
        issue("INVALID_INPUT", "question must be a nonempty string or null")
        return result
    if question is not None and len(question) > 2000:
        issue("LIMIT_EXCEEDED", "question exceeds 2000 characters")
        return result
    try:
        canonical = json.dumps(
            {"sources": sources, "claims": claims, "question": question},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        issue("INVALID_INPUT", "Inputs must be finite JSON data")
        return result
    # Covers quotes and claims as well as source text before rendering large data.
    if len(canonical) > 2_097_152:
        issue("LIMIT_EXCEEDED", "Combined input exceeds 2 MiB")
        return result
    result["input_fingerprint"] = hashlib.sha256(canonical).hexdigest()
    indexed = {}
    total_text = 0
    for source in sources:
        if not isinstance(source, dict) or not {"id", "text", "start_line", "complete"} <= set(source) or set(source) - {"id", "text", "start_line", "complete", "location", "title"}:
            issue("INVALID_SOURCE", "Source needs id/text/start_line/complete; optional location/title")
            continue
        sid = source["id"]
        if not isinstance(sid, str) or not _ID.fullmatch(sid):
            issue("INVALID_SOURCE", "Source id must be a safe reference identifier")
            continue
        if sid in indexed:
            issue("DUPLICATE_SOURCE", "Source id must be unique", source_id=sid)
            continue
        metadata_valid = True
        for name, limit in (("location", 4096), ("title", 512)):
            if name in source:
                value = source[name]
                if not isinstance(value, str) or not value.strip():
                    issue("INVALID_SOURCE", f"Source {name} must be a nonempty string", source_id=sid)
                    metadata_valid = False
                elif len(value) > limit:
                    issue("LIMIT_EXCEEDED", f"Source {name} exceeds its character budget", source_id=sid)
                    metadata_valid = False
        if not metadata_valid:
            continue
        text = source["text"]
        start = source["start_line"]
        complete = source["complete"]
        if not isinstance(text, str) or not text.strip():
            issue("INVALID_SOURCE", "Source text must be nonempty", source_id=sid)
            continue
        total_text += len(text)
        if len(text) > 65536 or total_text > 262144:
            issue("LIMIT_EXCEEDED", "Source text exceeds per-source or total budget", source_id=sid)
            continue
        if type(start) is not int or start < 1 or type(complete) is not bool or (complete and start != 1):
            issue("INVALID_SOURCE", "Use positive start_line; complete sources must start at 1", source_id=sid)
            continue
        lines = text.splitlines()
        indexed[sid] = {
            "lines": lines, "start": start, "end": start + len(lines) - 1, "complete": complete,
            "location": source.get("location"), "title": source.get("title"),
        }

    seen_claims = set()
    rendered_claims = []
    for claim in claims:
        if not isinstance(claim, dict) or set(claim) != {"id", "section", "kind", "text", "refs"}:
            issue("INVALID_CLAIM", "Claim fields must be id/section/kind/text/refs")
            continue
        cid = claim["id"]
        if not isinstance(cid, str) or not _ID.fullmatch(cid):
            issue("INVALID_CLAIM", "Claim id must be a safe identifier")
            continue
        if cid in seen_claims:
            issue("DUPLICATE_CLAIM", "Claim id must be unique", claim_id=cid)
            continue
        seen_claims.add(cid)
        if claim["section"] not in _SECTIONS or claim["kind"] not in _KINDS:
            issue("INVALID_CLAIM", "Unsupported section or kind", claim_id=cid)
            continue
        text = claim["text"]
        if not isinstance(text, str) or not text.strip():
            issue("INVALID_CLAIM", "Claim text must be nonempty", claim_id=cid)
            continue
        if len(text) > 2000:
            issue("LIMIT_EXCEEDED", "Claim text exceeds 2000 characters", claim_id=cid)
            continue
        if _multiple_sentences(text):
            issue("INVALID_CLAIM", "Use one atomic sentence per claim; split independent sentences and cite each", claim_id=cid)
            continue
        refs = claim["refs"]
        if not isinstance(refs, list) or not refs:
            issue("MISSING_REFERENCE", "Every kind needs supporting references, including inference/unknown", claim_id=cid)
            continue
        if len(refs) > 8:
            issue("LIMIT_EXCEEDED", "A claim may cite at most eight spans", claim_id=cid)
            continue
        locators = []
        for ref in refs:
            if not isinstance(ref, dict) or set(ref) != {"source_id", "start_line", "end_line", "quote"}:
                issue("INVALID_RANGE", "Reference fields must be source_id/start_line/end_line/quote", claim_id=cid)
                continue
            sid = ref["source_id"]
            if not isinstance(sid, str) or sid not in indexed:
                issue("UNKNOWN_SOURCE", "Reference must identify a valid supplied source", claim_id=cid,
                      source_id=sid if isinstance(sid, str) else None)
                continue
            source = indexed[sid]
            first, last = ref["start_line"], ref["end_line"]
            if type(first) is not int or type(last) is not int or not source["start"] <= first <= last <= source["end"]:
                issue("INVALID_RANGE", "Reference must be within supplied read range", claim_id=cid, source_id=sid)
                continue
            quote = ref["quote"]
            if not isinstance(quote, str) or not quote.strip():
                issue("QUOTE_MISMATCH", "Supply a nonempty exact quote from the cited span", claim_id=cid, source_id=sid)
                continue
            if len(quote) > 4096:
                issue("LIMIT_EXCEEDED", "Quote exceeds 4096 characters", claim_id=cid, source_id=sid)
                continue
            span = "\n".join(source["lines"][first - source["start"]:last - source["start"] + 1])
            if quote not in span:
                issue("QUOTE_MISMATCH", "Quote is not verbatim within the stated span", claim_id=cid, source_id=sid)
                continue
            if claim["kind"] == "omission" and (
                not source["complete"] or first != source["start"] or last != source["end"]
            ):
                issue("INCOMPLETE_OMISSION", "Omission requires complete-source declaration and its full inspected range", claim_id=cid, source_id=sid)
                continue
            locator = f"{sid}:{first}" if first == last else f"{sid}:{first}-{last}"
            if locator not in locators:
                locators.append(locator)
        prefix = {"inference": "Inference: ", "unknown": "Unknown: "}.get(claim["kind"], "")
        rendered_claims.append((claim["section"], f"{prefix}{_plain(text).rstrip('.!?')} ({'; '.join(locators)})."))
    if issues:
        return result
    parts = ["# Research Report"]
    for section in _SECTIONS:
        paragraphs = [text for name, text in rendered_claims if name == section]
        if section == "Scope" and question:
            paragraphs.insert(0, f"Question: {_plain(question)}")
        parts.extend([f"## {section}", "\n\n".join(paragraphs) or "Not supplied."])
    parts.extend(["## Sources", "\n".join(
        f"- {sid}:{source['start']}-{source['end']}"
        + (f" — {_plain(source['title'])}" if source["title"] else "")
        + (f" — {_plain(source['location'])}" if source["location"] else "")
        for sid, source in indexed.items()
    )])
    result["structural_valid"] = True
    result["rendered_markdown"] = "\n\n".join(parts) + "\n"
    return result


def build_research_review_tool():
    """Build the same stateless host tool for Native and External research children."""
    from openjiuwen.core.foundation.tool import LocalFunction, ToolCard

    reference = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "source_id": {"type": "string"},
            "start_line": {"type": "integer", "minimum": 1},
            "end_line": {"type": "integer", "minimum": 1},
            "quote": {"type": "string", "minLength": 1, "maxLength": 4096},
        },
        "required": ["source_id", "start_line", "end_line", "quote"],
    }
    return LocalFunction(
        card=ToolCard(
            name=_TOOL_NAME,
            description=(
                "Review supplied research evidence and claim spans, then render adjacent citations. "
                "Pure structural check: no files read/written, no fact verification or trusted read proof. "
                "Read original sources first. On issues, revise the claim table within the task budget. "
                "Use rendered_markdown verbatim after independent semantic review."
            ),
            input_params={
                "type": "object", "additionalProperties": False,
                "properties": {
                    "sources": {
                        "type": "array", "minItems": 1, "maxItems": 32,
                        "items": {
                            "type": "object", "additionalProperties": False,
                            "properties": {
                                "id": {"type": "string"},
                                "text": {"type": "string", "minLength": 1, "maxLength": 65536},
                                "start_line": {"type": "integer", "minimum": 1},
                                "complete": {"type": "boolean"},
                                "location": {"type": "string", "minLength": 1, "maxLength": 4096},
                                "title": {"type": "string", "minLength": 1, "maxLength": 512},
                            },
                            "required": ["id", "text", "start_line", "complete"],
                        },
                    },
                    "claims": {
                        "type": "array", "minItems": 1, "maxItems": 64,
                        "items": {
                            "type": "object", "additionalProperties": False,
                            "properties": {
                                "id": {"type": "string"},
                                "section": {"type": "string", "enum": list(_SECTIONS)},
                                "kind": {"type": "string", "enum": list(_KINDS)},
                                "text": {"type": "string", "minLength": 1, "maxLength": 2000},
                                "refs": {"type": "array", "minItems": 1, "maxItems": 8, "items": reference},
                            },
                            "required": ["id", "section", "kind", "text", "refs"],
                        },
                    },
                    "question": {"type": "string", "maxLength": 2000},
                },
                "required": ["sources", "claims"],
            },
        ),
        func=review_research_report,
    )


__all__ = ["review_research_report", "build_research_review_tool"]
