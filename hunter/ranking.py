"""Explainable attention ranking, not an appraisal or bidding recommendation."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from hunter.models import finite_number, parse_time, plain_text

MEDIA = r"\b(zeitschrift\w*|prospekt\w*|katalog\w*|bedienungsanleitung\w*|handbuch\w*|magazine?|heft|hefte|aufkleber|sticker)\b"
CONSUMABLE = r"\b(handschuh\w*|transistoren?|schraubensortiment\w*|dioden?|widerstände|widerstaende)\b"
ACCESSORY = r"\b(nur\s+gehäuse|nur\s+koffer|leerverpackung|leerkoffer|ersatzkabel|bedienknopf|abdeckung|ersatzteil|für\s+(?:einen\s+)?autoklav)\b"
DEVICE = r"(autoklav|oszilloskop|mikroskop|spektrumanalysator|funktionsgenerator|labornetzteil|schweißgerät|schweissgerät|drehmaschine|fräsmaschine|fraesmaschine|tischbohrmaschine|frequenzumrichter|werkstattpresse|laborzentrifuge|stereo\s*mikroskop|industrielle\s+waage)"
TOOL = r"(messgerät|messgeraet|steuergerät|steuergeraet|sps\b|akkuschrauber|stichsäge|stichsaege|bohrhammer|kompressor|drehmomentschlüssel|drehmomentschluessel)"
DAMAGE = r"\b(defekt\w*|ungeprüft|ungeprueft|bastler\w*|unvollständig|unvollstaendig|ersatzteil\w*)\b"


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))[:12]


def heuristic_assessment(item: dict[str, Any]) -> dict[str, Any]:
    title = str(item.get("title", "")).casefold()
    if re.search(MEDIA, title):
        kind, interest, band, confidence = "media", 4, "around_5", "medium"
        reason = "Drucksache oder Begleitmaterial, kein eigenständiges Gerät; seltene Sammlerausnahmen nicht geprüft."
    elif re.search(ACCESSORY, title):
        kind, interest, band, confidence = "accessory", 20, "unknown", "low"
        reason = "Möglicherweise nur Zubehör oder Ersatzteil statt des genannten Geräts; Ausführung und Vollständigkeit prüfen."
    elif re.search(CONSUMABLE, title) and not re.search(r"tester|prüfgerät|pruefgeraet|messgerät|messgeraet", title):
        kind, interest, band, confidence = "consumable", 6, "around_5", "medium"
        reason = "Kleinteile oder gewöhnliches Verbrauchsmaterial passen kaum zum Interesseprofil; Menge kann die Größenordnung ändern."
        if re.search(r"palette|großposten|grossposten|konvolut|\b[1-9]\d{2,}\b", title):
            band, confidence = "unknown", "low"
    elif re.search(DEVICE, title):
        kind, interest, band, confidence = "apparatus", 80, "around_500", "low"
        reason = "Der Titel deutet auf ein eigenständiges Spezialgerät hin; grobe Geräteklasse, kein belegter Gebrauchtwert."
    elif re.search(TOOL, title):
        kind, interest, band, confidence = "tool", 57, "around_50", "low"
        reason = "Nutzbares Werkzeug oder technische Baugruppe denkbar; Modell und Zustand können die Größenordnung stark ändern."
    else:
        kind, interest, band, confidence = "unknown", 35, "unknown", "low"
        reason = "Aus dem Titel ist die Geräte- und Wertklasse nicht zuverlässig erkennbar; für semantische Sichtung offenhalten."
    risks = []
    text = title + " " + str(item.get("condition", "")).casefold()
    if re.search(DAMAGE, text):
        interest = max(0, interest - 20)
        confidence = "low"
        risks.append("Defekt, ungeprüft oder unvollständig angegeben; keine Funktion unterstellen")
    if "autoklav" in title:
        risks.append("Zustands-, Wartungs- und Prüfunterlagen klären; Verkäuferangabe ist keine Funktions- oder Sicherheitsprüfung")
    return {"product_kind": kind, "semantic_interest": interest, "value_band": band,
            "assessment_confidence": confidence, "value_reason": reason,
            "reasons": [reason], "risks": risks,
            "next_checks": ["Genaue Modellbezeichnung, Lieferumfang und Zustand im Originalangebot prüfen"]}


def _score(semantic_interest: int, fact_points: int, eligibility: str, kind: str) -> int:
    score = min(100, max(0, round(semantic_interest * 0.7 + fact_points)))
    if kind in {"media", "consumable"}:
        score = min(score, 25)
    if eligibility == "excluded":
        score = min(score, 15)
    return score


def _provisional_pickup_match(item: dict[str, Any], settings: dict[str, Any]) -> bool:
    """Accept a matching aggregator radius query only as a reviewable lead.

    A query match is not a verified article location or pickup agreement. It may
    keep a candidate in review, but can never set local_pickup_confirmed.
    """
    postcode = settings.get("postcode")
    search_radius = finite_number(item.get("pickup_search_radius_km"))
    requested_radius = finite_number(settings.get("pickup_radius_km", 50))
    return (
        item.get("verification_level") == "aggregator"
        and item.get("source_provider") == "auktionen-ohne-gebot"
        and item.get("pickup_search_confirmed") is True
        and isinstance(postcode, str)
        and re.fullmatch(r"[0-9]{5}", postcode) is not None
        and item.get("pickup_search_postcode") == postcode
        and search_radius is not None and search_radius > 0
        and requested_radius is not None and requested_radius > 0
        and search_radius <= requested_radius
    )


def assess_item(item: dict[str, Any], settings: dict[str, Any], at: datetime) -> dict[str, Any]:
    result = dict(item)
    # Recomputing facts starts with the explicitly labelled fallback. Optional
    # API or interactive reviews must attach their own validated provenance.
    result.pop("semantic_review_mode", None)
    result.pop("semantic_assessed_at", None)
    reasons: list[str] = []
    risks: list[str] = []
    failed: list[str] = []
    unknown: list[str] = []
    price = finite_number(item.get("price_eur"))
    shipping = finite_number(item.get("shipping_eur"))
    distance = finite_number(item.get("distance_km"))
    local = item.get("local_pickup_confirmed") is True
    radius = float(settings.get("pickup_radius_km", 50))
    if local and distance is not None and distance > radius:
        local = False
    max_shipping = float(settings.get("max_shipping_eur", 15))
    delivery = shipping is not None and shipping <= max_shipping
    points = 0
    if price is None:
        unknown.append("Aktueller Auktionspreis unbekannt")
    elif price > float(settings.get("max_price_eur", 10)):
        failed.append("Aktueller Preis über dem Suchlimit")
    else:
        points += 12 if price <= 1 else 7
        reasons.append("Aktueller Auktionspreis höchstens 1 €" if price <= 1 else "Aktueller Auktionspreis unter dem Suchlimit")
    bids = item.get("bid_count")
    if bids is None:
        unknown.append("Gebotszahl unbekannt")
    elif bids > int(settings.get("max_bids", 0)):
        failed.append("Mehr Gebote als im Suchprofil erlaubt")
    elif bids == 0:
        points += 8
        reasons.append("Zum Abrufzeitpunkt keine Gebote")
    if local:
        points += 10
        reasons.append("Abholung im bestätigten Suchradius möglich")
    elif delivery:
        points += 8
        reasons.append("Ausgewiesene Versandkosten im Suchlimit")
    elif _provisional_pickup_match(item, settings):
        points += 8
        reasons.append("Nähe laut Umkreissuche der Quelle")
        unknown.append("Standort und Abholbedingungen im Original prüfen; Nähe laut Umkreissuche der Quelle")
    elif shipping is None:
        unknown.append("Weder Versandkosten noch passende Abholung bestätigt")
    else:
        # Unknown pickup is not proof that collection is impossible.
        failed.append("Versand über dem Limit; keine passende Abholung bestätigt")
    if item.get("is_auction") is False:
        failed.append("Keine Auktion")
    elif item.get("is_auction") is not True:
        unknown.append("Auktionsformat nicht bestätigt")
    end = parse_time(item.get("ends_at"))
    if not end:
        unknown.append("Ablaufdatum unbekannt")
    elif end <= at:
        failed.append("Angebot abgelaufen")
    elif end > at + timedelta(hours=float(settings.get("lookahead_hours", 24))):
        failed.append("Ablauf außerhalb des Suchfensters")
    seen = parse_time(item.get("last_seen_at"))
    if not seen or seen > at:
        unknown.append("Quellenstand unbekannt oder unplausibel")
    elif at - seen >= timedelta(hours=min(24, float(settings.get("max_freshness_hours", 24)))):
        failed.append("Quellenstand hat das zulässige Aufbewahrungsfenster erreicht")
    elif at - seen > timedelta(hours=2):
        risks.append("Quellenstand älter als zwei Stunden; Angaben können inzwischen abweichen")
    reserve = item.get("reserve_status", "unknown")
    if reserve == "not_met":
        failed.append("Versteckter Mindestpreis noch nicht erreicht")
    elif reserve == "unknown":
        unknown.append("Mindestpreisstatus unbekannt; keine Reservefreiheit unterstellen")
    elif reserve == "met":
        # Met is not 'none': preserve the user's no-reserve preference as a check.
        unknown.append("Mindestpreis erreicht, aber nicht als reservefrei bestätigt")
    if item.get("price_kind") == "auction_price":
        unknown.append("Auktionspreis nur aus allgemeinem Preisfeld; Gebotsdetails prüfen")
    if item.get("verification_level") == "aggregator":
        risks.append("Angaben aus zwischengespeicherter Fremdübersicht; Preis, Gebote und Versand im Originalangebot erneut prüfen")
    eligibility = "excluded" if failed else ("review" if unknown else "eligible")
    assessment = heuristic_assessment(item)
    fact_points = max(0, points - (3 if unknown else 0))
    result.update(assessment)
    result.update({
        "eligibility": eligibility, "eligibility_reasons": failed + unknown,
        "semantic_assessed": True, "assessment_basis": "heuristic",
        "fact_score": fact_points, "local_pickup_confirmed": local,
        "fact_reasons": _unique(reasons), "fact_risks": _unique(failed + unknown + risks),
        "reasons": _unique(reasons + assessment["reasons"]),
        "risks": _unique(failed + unknown + risks + assessment["risks"]),
        "total_known_eur": round(price + shipping, 2) if price is not None and shipping is not None else None,
    })
    result["score"] = result["interest_score"] = _score(assessment["semantic_interest"], fact_points, eligibility, assessment["product_kind"])
    return result


def apply_semantic(item: dict[str, Any], assessment: dict[str, Any]) -> dict[str, Any]:
    """Model output can annotate/reorder a candidate, never change source facts."""
    score = assessment.get("semantic_interest")
    if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100:
        raise ValueError("Invalid semantic interest")
    band = assessment.get("value_band")
    confidence = assessment.get("assessment_confidence")
    kind = assessment.get("product_kind", "unknown")
    if band not in {"around_5", "around_50", "around_500", "unknown"}:
        raise ValueError("Invalid value band")
    if confidence not in {"low", "medium", "high"}:
        raise ValueError("Invalid confidence")
    if kind not in {"apparatus", "tool", "accessory", "media", "consumable", "unknown"}:
        kind = "unknown"
    result = dict(item)
    result.update({"semantic_interest": score, "value_band": band,
                   "assessment_confidence": confidence, "product_kind": kind,
                   "value_reason": plain_text(assessment.get("value_reason"), 600),
                   "semantic_assessed": True, "assessment_basis": "llm"})
    for field in ("reasons", "risks", "next_checks"):
        values = assessment.get(field)
        values = [plain_text(v, 300) for v in values if isinstance(v, str)] if isinstance(values, list) else []
        # New semantic evidence replaces the old heuristic, not source facts.
        factual = item.get("fact_" + field, []) if field in {"reasons", "risks"} else []
        result[field] = _unique(list(factual) + values)
    result["score"] = result["interest_score"] = _score(score, int(item.get("fact_score", 0)), item.get("eligibility", "review"), kind)
    return result
