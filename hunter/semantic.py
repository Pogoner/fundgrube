"""Optional, bounded semantic screening; source facts are never model outputs.

The module makes no network request without an explicit opt-in, key and model.
Only the three documented source text fields and an item ID enter a request.
Economic size classes are rough screening signals, not appraisals or bid advice.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

ENDPOINT = "https://api.openai.com/v1/responses"
PROMPT_VERSION = "fundgrube-semantic-v1"
PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "semantic-screening.md"
DOCUMENTATION_MARKER = "<!-- Betriebshinweis:"
MAX_RESPONSE_BYTES = 1_000_000
MAX_INPUT_DESCRIPTION = 4_000
MAX_BATCH_SIZE = 10
MAX_ITEMS_PER_RUN = 20
MAX_CACHE_ITEMS = 800
VALUE_BANDS = {"around_5", "around_50", "around_500", "unknown"}
CONFIDENCES = {"low", "medium", "high"}
PRODUCT_KINDS = {"apparatus", "tool", "accessory", "media", "consumable", "unknown"}
ASSESSMENT_FIELDS = {
    "value_band", "semantic_interest", "assessment_confidence", "value_reason",
    "reasons", "risks", "next_checks", "product_kind",
}
ANNOTATION_FIELDS = ASSESSMENT_FIELDS | {
    "interest_score", "score", "semantic_assessed", "assessment_basis",
}
Transport = Callable[[urllib.request.Request, float], dict[str, Any]]


class SemanticError(RuntimeError):
    """A safe, non-secret-bearing screening failure suitable for status output."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _utc(at: datetime) -> datetime:
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("Semantic screening needs a timezone-aware datetime")
    return at.astimezone(timezone.utc)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return _utc(result)
    except (ValueError, OverflowError):
        return None


def _iso(at: datetime) -> str:
    return at.isoformat(timespec="seconds").replace("+00:00", "Z")


def _integer(value: Any, default: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        if isinstance(value, float) and not math.isfinite(value):
            return default
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return min(maximum, max(minimum, number))


def _bounded_text(value: Any, length: int) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)[:length]


def _prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8").split(DOCUMENTATION_MARKER, 1)[0].strip()


def _input_item(item: dict[str, Any]) -> dict[str, str]:
    # Whitelist, never serialize the source object, settings, state or environment.
    return {
        "id": str(item["id"]),
        "title": _bounded_text(item.get("title"), 300),
        "description": _bounded_text(item.get("description"), MAX_INPUT_DESCRIPTION),
        "condition": _bounded_text(item.get("condition"), 160),
    }


def _fingerprint(item: dict[str, Any], model: str, prompt: str) -> str:
    data = {"input": _input_item(item), "model": model, "prompt": prompt, "version": PROMPT_VERSION}
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _schema(ids: list[str]) -> dict[str, Any]:
    short_list = {
        "type": "array", "items": {"type": "string", "maxLength": 240}, "maxItems": 4,
    }
    properties = {
        "id": {"type": "string", "enum": ids},
        "value_band": {"type": "string", "enum": sorted(VALUE_BANDS)},
        "semantic_interest": {"type": "integer", "minimum": 0, "maximum": 100},
        "assessment_confidence": {"type": "string", "enum": sorted(CONFIDENCES)},
        "product_kind": {"type": "string", "enum": sorted(PRODUCT_KINDS)},
        "value_reason": {"type": "string", "maxLength": 500},
        "reasons": short_list,
        "risks": short_list,
        "next_checks": {
            "type": "array", "items": {"type": "string", "maxLength": 240}, "maxItems": 5,
        },
    }
    return {
        "type": "object", "additionalProperties": False, "required": ["assessments"],
        "properties": {"assessments": {
            "type": "array", "minItems": len(ids), "maxItems": len(ids),
            "items": {"type": "object", "additionalProperties": False,
                      "properties": properties, "required": list(properties)},
        }},
    }


def _payload(items: list[dict[str, Any]], model: str, prompt: str, max_output_tokens: int) -> dict[str, Any]:
    return {
        "model": model,
        "instructions": prompt,
        "input": [{"role": "user", "content": [{
            "type": "input_text",
            "text": json.dumps({"untrusted_listing_data": [_input_item(item) for item in items]},
                               ensure_ascii=False, separators=(",", ":")),
        }]}],
        "store": False,
        "tools": [],
        "max_output_tokens": max_output_tokens,
        "text": {"format": {
            "type": "json_schema", "name": "fundgrube_semantic_screening", "strict": True,
            "schema": _schema([item["id"] for item in items]),
        }},
    }


def _default_transport(request: urllib.request.Request, timeout: float) -> dict[str, Any]:
    opener = urllib.request.build_opener(_NoRedirect())
    with opener.open(request, timeout=timeout) as response:
        data = response.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise SemanticError("OpenAI-Antwort überschreitet die erlaubte Größe.")
    decoded = json.loads(data.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise SemanticError("OpenAI-Antwort hat ein unerwartetes Format.")
    return decoded


def _request(payload: dict[str, Any], api_key: str, timeout: float, transport: Transport) -> dict[str, Any]:
    request = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                 "Accept": "application/json", "User-Agent": "Fundgrube/2 semantic-screening"},
        method="POST",
    )
    try:
        result = transport(request, timeout)
    except urllib.error.HTTPError as error:
        # Never include a response body, URL, headers or upstream error message.
        raise SemanticError(f"OpenAI-Anfrage fehlgeschlagen (HTTP {int(error.code)}).") from None
    except SemanticError:
        raise
    except (OSError, urllib.error.URLError, ValueError, TypeError, UnicodeError):
        raise SemanticError("OpenAI-Anfrage fehlgeschlagen; Verbindung oder Antwort prüfen.") from None
    if not isinstance(result, dict):
        raise SemanticError("OpenAI-Antwort hat ein unerwartetes Format.")
    return result


def _validate_assessment(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != ASSESSMENT_FIELDS:
        raise SemanticError("Modellbewertung enthält fehlende oder unerwartete Felder.")
    score = value.get("semantic_interest")
    if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
        raise SemanticError("Modellbewertung enthält einen ungültigen Interesse-Score.")
    if (not isinstance(value.get("value_band"), str) or value["value_band"] not in VALUE_BANDS
            or not isinstance(value.get("assessment_confidence"), str) or value["assessment_confidence"] not in CONFIDENCES
            or not isinstance(value.get("product_kind"), str) or value["product_kind"] not in PRODUCT_KINDS):
        raise SemanticError("Modellbewertung enthält eine unbekannte Klasse.")
    reason = value.get("value_reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
        raise SemanticError("Modellbewertung enthält keine gültige Kurzbegründung.")
    result = {key: value[key] for key in ASSESSMENT_FIELDS - {"reasons", "risks", "next_checks"}}
    result["value_reason"] = _bounded_text(reason, 500)
    for key, maximum in (("reasons", 4), ("risks", 4), ("next_checks", 5)):
        entries = value.get(key)
        if (not isinstance(entries, list) or len(entries) > maximum
                or any(not isinstance(entry, str) or not entry.strip() or len(entry) > 240 for entry in entries)):
            raise SemanticError("Modellbewertung enthält eine ungültige Begründungsliste.")
        result[key] = [_bounded_text(entry, 240) for entry in entries]
    return result


def _assessments(response: dict[str, Any], ids: list[str]) -> dict[str, dict[str, Any]]:
    if response.get("status") != "completed" or response.get("error"):
        raise SemanticError("Modellantwort ist unvollständig oder fehlgeschlagen; Heuristik bleibt erhalten.")
    chunks = []
    output = response.get("output")
    if not isinstance(output, list):
        raise SemanticError("Modellantwort enthält keine vollständige strukturierte Ausgabe.")
    for part in output:
        if not isinstance(part, dict):
            raise SemanticError("Modellantwort enthält eine ungültige Ausgabe.")
        if part.get("type") == "reasoning":
            continue
        if part.get("type") != "message" or part.get("role") != "assistant":
            raise SemanticError("Modellantwort enthält unerwartete Aktionen statt einer Bewertung.")
        content = part.get("content")
        if not isinstance(content, list):
            raise SemanticError("Modellantwort enthält eine ungültige Ausgabe.")
        for block in content:
            if not isinstance(block, dict):
                raise SemanticError("Modellantwort enthält eine ungültige Ausgabe.")
            if block.get("type") == "refusal":
                raise SemanticError("Das Modell hat die Bewertung abgelehnt; Heuristik bleibt erhalten.")
            if block.get("type") != "output_text" or not isinstance(block.get("text"), str):
                raise SemanticError("Modellantwort enthält keine gültige Textausgabe.")
            chunks.append(block["text"])
    try:
        result = json.loads("".join(chunks))
    except (ValueError, TypeError):
        raise SemanticError("Modellantwort enthält kein gültiges Bewertungs-JSON.") from None
    if not isinstance(result, dict) or set(result) != {"assessments"} or not isinstance(result["assessments"], list):
        raise SemanticError("Modellantwort entspricht nicht dem Bewertungsschema.")
    assessments = {}
    expected = set(ids)
    for value in result["assessments"]:
        if not isinstance(value, dict) or set(value) != ASSESSMENT_FIELDS | {"id"}:
            raise SemanticError("Modellbewertung enthält fehlende oder unerwartete Felder.")
        item_id = value.get("id")
        if not isinstance(item_id, str) or item_id not in expected or item_id in assessments:
            raise SemanticError("Modellantwort enthält eine fremde oder doppelte Angebots-ID.")
        assessments[item_id] = _validate_assessment({key: value[key] for key in ASSESSMENT_FIELDS})
    if set(assessments) != expected:
        raise SemanticError("Modellantwort lässt Angebote aus; unvollständige Charge verworfen.")
    return assessments


def _usage(response: dict[str, Any] | None) -> int | None:
    usage = response.get("usage") if isinstance(response, dict) else None
    if not isinstance(usage, dict):
        return None
    total = usage.get("total_tokens")
    if isinstance(total, int) and not isinstance(total, bool) and 0 <= total <= 10_000_000:
        return total
    input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
    if all(isinstance(n, int) and not isinstance(n, bool) and 0 <= n <= 5_000_000
           for n in (input_tokens, output_tokens)):
        return input_tokens + output_tokens
    return None


def _reservation(payload: dict[str, Any]) -> int:
    # UTF-8 bytes + generous framing allowance deliberately overestimates normal
    # tokenization; actual returned usage replaces it after each request. This is
    # an application admission budget, not a provider-enforced spend guarantee.
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    return len(encoded) + int(payload["max_output_tokens"]) + 1_024


def _apply(item: dict[str, Any], assessment: dict[str, Any], assessed_at: str) -> dict[str, Any]:
    from hunter.ranking import apply_semantic

    enriched = apply_semantic(item, assessment)
    result = dict(item)
    # Belt and suspenders: scoring cannot overwrite facts even if its helper
    # accidentally starts returning altered source fields in a future change.
    result.update({key: enriched[key] for key in ANNOTATION_FIELDS if key in enriched})
    result["semantic_assessed_at"] = assessed_at
    return result


def enrich_semantics(
    items: list[dict[str, Any]], settings: dict[str, Any], state: dict[str, Any], at: datetime,
    *, transport: Transport | None = None, environ: Mapping[str, str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Add cached/new annotations to fresh candidates, preserving every source fact.

    Only ``state['semantic_budget']`` and ``state['semantic_cache']`` are changed.
    A transport receives a fixed-origin Request and timeout, making tests fully
    offline. HTTP retries are intentionally absent: every attempt uses budget.
    """
    at = _utc(at)
    env = os.environ if environ is None else environ
    output = [dict(item) for item in items]
    metadata: dict[str, Any] = {
        "ai_enabled": False, "llm_status": "disabled", "calls_used": 0,
        "cache_hits": 0, "assessed": 0, "fresh_assessments": 0, "errors": [],
        "budget_exhausted": False, "run_limit_reached": False,
        "token_budget_is_estimate": True,
    }
    active: list[tuple[int, dict[str, Any]]] = []
    seen_ids = set()
    freshness = min(6, _integer(settings.get("max_freshness_hours"), 6, 1, 6))
    for index, item in enumerate(output):
        item_id = item.get("id")
        seen = _parse_time(item.get("last_seen_at"))
        end = _parse_time(item.get("ends_at"))
        if (not isinstance(item_id, str) or not item_id or len(item_id) > 200 or item_id in seen_ids
                or item.get("eligibility") == "excluded" or seen is None or end is None
                or end <= at or seen > at + timedelta(minutes=5) or at - seen > timedelta(hours=freshness)):
            continue
        seen_ids.add(item_id)
        active.append((index, item))
    active = active[:min(MAX_CACHE_ITEMS, _integer(settings.get("max_records"), 800, 1, MAX_CACHE_ITEMS))]
    active_ids = {item["id"] for _, item in active}
    stored_cache = state.get("semantic_cache")
    stored_cache = stored_cache if isinstance(stored_cache, dict) else {}
    previous_cache = {}
    for key, value in stored_cache.items():
        if key not in active_ids or not isinstance(value, dict):
            continue
        try:
            assessment = _validate_assessment(value.get("assessment"))
            stamp = _parse_time(value.get("assessed_at"))
            fingerprint = value.get("fingerprint")
            cached_model = value.get("model")
            if (stamp is None or stamp > at + timedelta(minutes=5)
                    or not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)
                    or not isinstance(cached_model, str) or len(cached_model) > 200
                    or value.get("prompt_version") != PROMPT_VERSION):
                continue
            previous_cache[key] = {"fingerprint": fingerprint, "model": cached_model,
                                   "prompt_version": PROMPT_VERSION, "assessment": assessment,
                                   "assessed_at": _iso(stamp)}
        except (SemanticError, TypeError):
            continue
    state["semantic_cache"] = previous_cache
    if str(env.get("HUNTER_LLM_ENABLED", "")).strip().casefold() != "true":
        return output, metadata
    api_key = str(env.get("OPENAI_API_KEY", "")).strip()
    model = str(env.get("OPENAI_MODEL", "")).strip()
    if (not api_key or len(api_key) > 1_024 or any(character.isspace() for character in api_key)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", model)):
        metadata.update({"llm_status": "unconfigured",
                         "errors": ["LLM-Sichtung benötigt OPENAI_API_KEY und ein gültiges OPENAI_MODEL."]})
        return output, metadata
    metadata["ai_enabled"] = True
    prompt = _prompt()
    budget = state.get("semantic_budget")
    today = at.date().isoformat()
    if not isinstance(budget, dict) or budget.get("date") != today:
        budget = {"date": today, "requests": 0, "tokens": 0, "reserved_tokens": 0}
    else:
        budget = {"date": today,
                  **{key: _integer(budget.get(key), 0, 0, 100_000_000)
                     for key in ("requests", "tokens", "reserved_tokens")}}
    state["semantic_budget"] = budget
    request_limit = _integer(settings.get("semantic_daily_requests"), 12, 0, 96)
    token_limit = _integer(settings.get("semantic_daily_tokens"), 120_000, 0, 2_000_000)
    batch_limit = _integer(settings.get("semantic_max_batches_per_run"), 2, 0, 2)
    item_limit = _integer(settings.get("max_ai_items_per_run"), 20, 0, MAX_ITEMS_PER_RUN)
    batch_size = _integer(settings.get("semantic_batch_size"), 10, 1, MAX_BATCH_SIZE)
    max_output_tokens = _integer(settings.get("semantic_max_output_tokens"), 6_000, 1_000, 12_000)
    timeout = float(_integer(settings.get("semantic_timeout_seconds"), 25, 1, 30))
    cache: dict[str, Any] = {}
    pending: list[tuple[int, dict[str, Any], str]] = []
    for index, item in active:
        fingerprint = _fingerprint(item, model, prompt)
        cached = previous_cache.get(item["id"])
        try:
            if (not isinstance(cached, dict) or cached.get("fingerprint") != fingerprint
                    or cached.get("model") != model or cached.get("prompt_version") != PROMPT_VERSION):
                raise SemanticError("Cache miss")
            assessed_at = _parse_time(cached.get("assessed_at"))
            if assessed_at is None or assessed_at > at + timedelta(minutes=5):
                raise SemanticError("Invalid cache time")
            assessment = _validate_assessment(cached.get("assessment"))
        except (SemanticError, TypeError):
            pending.append((index, item, fingerprint))
            continue
        cache[item["id"]] = {"fingerprint": fingerprint, "model": model,
                             "prompt_version": PROMPT_VERSION, "assessment": assessment,
                             "assessed_at": _iso(assessed_at)}
        output[index] = _apply(item, assessment, _iso(assessed_at))
        metadata["cache_hits"] += 1
        metadata["assessed"] += 1
    state["semantic_cache"] = cache
    pending.sort(key=lambda entry: -_integer(entry[1].get("interest_score", entry[1].get("score")), 0, 0, 100))
    selected = pending[:item_limit]
    completed_ids: set[str] = set()
    for start in range(0, len(selected), batch_size):
        if metadata["calls_used"] >= batch_limit:
            metadata["run_limit_reached"] = True
            break
        if budget["requests"] >= request_limit:
            metadata["budget_exhausted"] = True
            break
        batch = selected[start:start + batch_size]
        payload = _payload([item for _, item, _ in batch], model, prompt, max_output_tokens)
        reservation = _reservation(payload)
        if budget["tokens"] + budget["reserved_tokens"] + reservation > token_limit:
            metadata["budget_exhausted"] = True
            break
        budget["requests"] += 1
        budget["reserved_tokens"] += reservation
        metadata["calls_used"] += 1
        response = None
        try:
            response = _request(payload, api_key, timeout, transport or _default_transport)
            answers = _assessments(response, [item["id"] for _, item, _ in batch])
        except SemanticError as error:
            metadata["errors"].append(str(error))
            # Stop instead of repeatedly charging for authentication, schema or
            # service failures. A later workflow can retry under the same budget.
            break
        else:
            for index, item, fingerprint in batch:
                assessment = answers[item["id"]]
                stamp = _iso(at)
                output[index] = _apply(item, assessment, stamp)
                cache[item["id"]] = {"fingerprint": fingerprint, "model": model,
                                     "prompt_version": PROMPT_VERSION, "assessment": assessment,
                                     "assessed_at": stamp}
                completed_ids.add(item["id"])
                metadata["assessed"] += 1
                metadata["fresh_assessments"] += 1
        finally:
            budget["reserved_tokens"] -= reservation
            used = _usage(response)
            budget["tokens"] += reservation if used is None else used
    if len(pending) > item_limit:
        metadata["run_limit_reached"] = True
    incomplete = len(completed_ids) < len(pending)
    metadata["queued"] = len(pending) - len(completed_ids)
    metadata["requests_used_today"] = budget["requests"]
    metadata["tokens_used_today"] = budget["tokens"]
    metadata["daily_request_budget"] = request_limit
    metadata["daily_token_budget"] = token_limit
    metadata["llm_status"] = (
        "error" if metadata["errors"] and not metadata["assessed"]
        else "partial" if incomplete or metadata["errors"]
        else "ok"
    )
    return output, metadata
