"""Explainable attention ranking, not an appraisal or bidding recommendation."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from hunter.models import finite_number, parse_time, plain_text

MEDIA = re.compile(
    r"\b(?:zeitschrift(?:en)?|[a-zäöüß]{0,48}(?:prospekt(?:e|en|s)?|katalog(?:e|en|s)?|preisliste(?:n)?|"
    r"anleitung(?:en)?|handb(?:uch|ücher|uecher)(?:s|n)?)|"
    r"prospekt(?:blatt|bl(?:ä|ae)tter(?:n)?|typ(?:en)?|heft(?:e|en)?)|"
    r"(?:daten|falt)bl(?:att|(?:ä|ae)tter(?:n)?)|flyer(?:n)?|parts\s+manual(?:s)?|gesamt(?:ü|ue)bersicht|"
    r"brosch(?:ü|ue)re(?:n)?|"
    r"(?:koch|sach|lehr|ern(?:ä|ae)hrungs|taschen|klapp|garten|daten)buch|"
    r"magazin(?:e)?|magazine|heft(?:e|en)?|werbebl(?:att|ätter|aetter)|aufkleber|sticker)\b", re.I)
# German compounds are identified by the document HEAD at the end of a whole
# word: Kurzkatalog is a catalog; Kataloghalter/Anleitungshalter are not. The
# bounded prefix avoids an ever-growing manufacturer/document-prefix list.
# Only whole media nouns: Prospekthalter/Broschürenständer are physical objects.
# Separated spellings and "Halter für Prospekte" need the same protection.
MEDIA_HARDWARE = re.compile(
    r"\b(?:prospekt(?:e|en)?|brosch(?:ü|ue)re(?:n)?|katalog(?:e|en)?|zeitschrift(?:en)?|flyer)"
    r"[\s-]*(?:halter\w*|st(?:ä|ae)nder\w*|h(?:ü|ue)lle(?:n)?\w*|regal\w*|spender\w*|display\w*)\b", re.I)
MEDIA_CONTAINER_PREFIX = re.compile(
    r"\b(?:halter\w*|st(?:ä|ae)nder\w*|regal\w*|spender\w*|display\w*)\s+(?:für|fuer)\s*$", re.I)
MEDIA_COMPLEMENT_PREFIX = re.compile(
    r"(?:\b(?:mit|inkl(?:usive)?\.?|einschlie(?:ß|ss)lich|samt|plus|ohne)"
    r"(?:\s+[\w.-]+){0,5}|(?<!\S)[+&])\s*$", re.I)
# A symbolic separator must stand alone directly before the complement noun.
# POWER+ is a model suffix; '& MÄRZ 2006 … BROSCHÜRE' joins subjects in a media
# title. Neither is evidence that a whole machine includes a bundled manual.
PART_FOR = re.compile(r"\b(?:ersatz|glas|bau)?teil(?:e|en)?(?:\s+[\w./()-]+){0,3}\s+(?:für|fuer)\b", re.I)
PART_CONDITION_PREFIX = re.compile(r"\bals(?:\s+(?:ein|reines?))?\s*$", re.I)
PERSONAL_MEDIA_REASON = "Nicht im persönlichen Suchprofil: eigenständig angebotene Drucksache oder Medienartikel, kein Gerät."
CONSUMABLE = r"\b(handschuh\w*|schraubensortiment\w*)\b"
SMALL_COMPONENT = r"\b(transistoren?|dioden?|widerstände|widerstaende)\b"
ACCESSORY = r"(?:^\s*(?:\d+\s+)?ersatzteile?\s+für\b|\b(nur\s+gehäuse|nur\s+koffer|leerverpackung|leerkoffer|ersatzkabel|bedienknopf|abdeckung|für\s+(?:einen\s+)?autoklav)\b)"
COMPONENT = r"\b(frequenzumrichter|steuergerät\w*|steuergeraet\w*|steuerplatine\w*|sps|sensortransmitter|messumformer|rma\s*42)\b"
DEVICE = r"(autoklav|oszilloskop|mikroskop|spektrumanalysator|funktionsgenerator|labornetzteil|schweißgerät|schweissgerät|drehmaschine|fräsmaschine|fraesmaschine|tischbohrmaschine|werkstattpresse|laborzentrifuge|stereo\s*mikroskop|industrielle\s+waage)"
TOOL = r"(messgerät|messgeraet|akkuschrauber|stichsäge|stichsaege|bohrmaschine|bohrhammer|kompressor|drehmomentschlüssel|drehmomentschluessel)"
MATERIAL = r"\b(sockelleisten?|aluprofile?|aluminiumprofile?|stahlprofile?|rohre|bleche)\b"
DAMAGE = r"\b(defekt\w*|ungeprüft|ungeprueft|bastler\w*|unvollständig|unvollstaendig|ersatzteil\w*)\b"
LEGACY_USEABILITY_REASON = "Useability wurde in dieser älteren Bewertung noch nicht beurteilt."
ATTACHMENT_NOUNS = (
    ("blade", re.compile(r"\bstichs(?:ä|ae)ge(?:n)?[\s-]*bl(?:att|ätter(?:n)?|aetter(?:n)?)\b", re.I)),
    ("chuck", re.compile(r"\b(?:schnellspann)?bohrfutter\b", re.I)),
    ("welding_tip", re.compile(r"\b(?:(?:strom|gas|kontakt)?d(?:ü|ue)se(?:n)?|(?:kontakt|schwei(?:ß|ss))?spitze(?:n)?)\b", re.I)),
    ("machine_monitor", re.compile(r"\bmonitor(?:e)?\b", re.I)),
)


def _standalone_media(title: str) -> bool:
    """Recognize the offered noun, not a machine's bundled/missing paperwork.

    This is deliberately a conservative, transparent title guard, not an LLM
    judgment. Exact noun boundaries protect compounds such as Prospekthalter.
    Each mention is checked separately: "Prospekt mit Bedienungsanleitung"
    remains media, but "Bohrmaschine mit Koffer und Bedienungsanleitung" does not.
    """
    hardware = list(MEDIA_HARDWARE.finditer(title))
    for mention in MEDIA.finditer(title):
        prefix = title[:mention.start()]
        suffix = title[mention.end():]
        if any(match.start() <= mention.start() < match.end() for match in hardware):
            continue
        if MEDIA_CONTAINER_PREFIX.search(prefix) or MEDIA_COMPLEMENT_PREFIX.search(prefix):
            continue
        if re.match(r"\s+(?:fehlt|fehlen|nicht\s+(?:dabei|vorhanden|enthalten))\b", suffix, re.I):
            continue
        return True
    return False


def _offered_part(title: str) -> bool:
    """'Teil E1 für …' is a part; 'Gerät als Ersatzteil für Bastler' is not."""
    for mention in PART_FOR.finditer(title):
        prefix = title[:mention.start()]
        if PART_CONDITION_PREFIX.search(prefix) or MEDIA_COMPLEMENT_PREFIX.search(prefix):
            continue
        if re.match(r"\s*(?:bastler\w*|ersatzteilgewinnung)\b", title[mention.end():], re.I):
            continue
        return True
    return False


def _offered_attachment(title: str) -> str | None:
    """Head nouns beat the referenced machine, but bundled fittings do not.

    Keep these guards narrow: a generic monitor is not automatically a machine
    component, and a nozzle mention requires a welding context. No parts are
    excluded from the user's profile by this fallback classification.
    """
    for kind, pattern in ATTACHMENT_NOUNS:
        if kind == "machine_monitor" and not re.search(DEVICE + r"|\bcnc\b", title, re.I):
            continue
        if kind == "welding_tip" and not re.search(r"schwei(?:ß|ss)(?:gerät|geraet|brenner)|\b(?:mig|mag)\b", title, re.I):
            continue
        for mention in pattern.finditer(title):
            if MEDIA_COMPLEMENT_PREFIX.search(title[:mention.start()]):
                continue
            return kind
    return None


def apply_interest_profile(item: dict[str, Any]) -> dict[str, Any]:
    """Recompute user exclusions separately from source facts and score axes.

    It must run after every semantic/review overlay. Even an old high-scoring
    review cannot turn an explicit brochure into a wanted machine. Conversely,
    this guard does not rewrite that review's scores, provenance or timestamp.
    """
    result = dict(item)
    title = str(item.get("title", ""))
    media = _standalone_media(title)
    contrary_title = (bool(MEDIA.search(title)) and not media) or bool(MEDIA_HARDWARE.search(title)) or _offered_part(title)
    excluded = media or (item.get("product_kind") == "media" and not contrary_title)
    result["personal_interest_excluded"] = bool(excluded)
    result["personal_interest_reason"] = PERSONAL_MEDIA_REASON if excluded else None
    return result


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))[:12]


def heuristic_assessment(item: dict[str, Any]) -> dict[str, Any]:
    """Illustrative keyword fallback, not a substitute for semantic LLM review.

    Autonomy describes the object's role and dependencies, not whether this
    specimen works. A defective complete device keeps its structural autonomy;
    an explicitly missing assembly is a different, integration-dependent item.
    """
    title = str(item.get("title", "")).casefold()
    attachment = _offered_attachment(title)
    dependencies: list[str] = []
    if _standalone_media(title):
        kind, scalp, useability, band, confidence = "media", 4, 95, "around_5", "medium"
        reason = "Drucksache oder Begleitmaterial, kein eigenständiges Gerät; seltene Sammlerausnahmen nicht geprüft."
        use_reason = "Als Lektüre eigenständig nutzbar; diese Unabhängigkeit macht gewöhnliche Drucksachen nicht wirtschaftlich interessant."
    elif _offered_part(title):
        kind, scalp, useability, band, confidence = "component", 20, 8, "unknown", "low"
        reason = "Angeboten wird ausdrücklich ein Teil für ein anderes Gerät; dessen Gerätename beschreibt nicht den Lieferumfang."
        use_reason = "Das genannte Teil benötigt das genau passende Hauptgerät; es ist selbst kein eigenständig nutzbares Gesamtgerät."
        dependencies = ["Passendes Hauptgerät und kompatible Ausführung des Teils"]
    elif attachment:
        band, confidence = "unknown", "low"
        if attachment == "machine_monitor":
            kind, scalp, useability = "component", 40, 15
            reason = "Der angebotene Gegenstand ist der Monitor einer Maschine, nicht die im Titel genannte vollständige Drehmaschine oder Anlage."
            use_reason = "Maschinenmonitor mit ungeklärten Anschlüssen und Steuerungskompatibilität; benötigt eine passende Anlage."
            dependencies = ["Passende Maschinensteuerung, Schnittstellen und Versorgung"]
        elif attachment == "chuck":
            kind, scalp, useability = "accessory", 25, 45
            reason = "Angeboten wird ein Bohrfutter als Werkzeugzubehör, nicht die erwähnte vollständige Bohrmaschine; Ausführung und Qualität bleiben offen."
            use_reason = "Das Bohrfutter benötigt eine Bohrmaschine oder Spindel mit passender Aufnahme; kein eigenständiger Antrieb."
            dependencies = ["Bohrmaschine oder Spindel mit kompatibler Aufnahme"]
        elif attachment == "blade":
            kind, scalp, useability = "consumable", 14, 40
            reason = "Angeboten werden Stichsägeblätter, keine vollständige Stichsäge; Menge, Werkstoff und Ausführung sind gesondert zu prüfen."
            use_reason = "Sägeblätter sind wechselbare Schneidmittel und benötigen eine passende Stichsäge und Blattaufnahme."
            dependencies = ["Passende Stichsäge und kompatible Blattaufnahme"]
        else:
            kind, scalp, useability = "consumable", 14, 35
            reason = "Angeboten werden Schweißspitzen oder Düsen, nicht das im Titel genannte vollständige Schweißgerät; Menge und Ausführung prüfen."
            use_reason = "Spitzen und Düsen benötigen einen passenden Schweißbrenner sowie ein Schweißgerät; Gewinde und Drahtmaß müssen passen."
            dependencies = ["Kompatibler Schweißbrenner und Schweißgerät", "Passende Gewinde und Drahtabmessungen"]
    elif re.search(ACCESSORY, title):
        kind, scalp, useability, band, confidence = "accessory", 20, 10, "unknown", "low"
        reason = "Möglicherweise nur Zubehör oder Ersatzteil statt des genannten Geräts; Ausführung und Vollständigkeit prüfen."
        use_reason = "Der benannte Gegenstand braucht voraussichtlich ein passendes Hauptgerät; dessen Kompatibilität ist nicht belegt."
        dependencies = ["Passendes Hauptgerät und kompatible Ausführung"]
    elif re.search(CONSUMABLE, title) and not re.search(r"tester|prüfgerät|pruefgeraet|messgerät|messgeraet", title):
        kind, scalp, useability, band, confidence = "consumable", 6, 95, "around_5", "medium"
        reason = "Kleinteile oder gewöhnliches Verbrauchsmaterial passen kaum zum Interesseprofil; Menge kann die Größenordnung ändern."
        use_reason = "Gewöhnliches Verbrauchsmaterial braucht typischerweise keine spezielle Maschine; Eignung für den Zweck bleibt zu prüfen."
        if re.search(r"palette|großposten|grossposten|konvolut|\b[1-9]\d{2,}\b", title):
            scalp, band, confidence = 30, "unknown", "low"
        if "schraubensortiment" in title:
            useability = 55
            use_reason = "Allgemeines Befestigungsmaterial braucht eine konkrete Montageaufgabe mit passenden Gewinden und Maßen."
            dependencies = ["Passende Montageaufgabe, Gewinde und Maße"]
    elif re.search(SMALL_COMPONENT, title) and not re.search(r"tester|prüfgerät|pruefgeraet|messgerät|messgeraet", title):
        kind, scalp, useability, band, confidence = "component", 7, 40, "around_5", "medium"
        reason = "Gewöhnliche kleine Elektronikbauteile sind einzeln wenig interessant; Stückzahl und genaue Ausführung können das ändern."
        use_reason = "Das Bauteil erfüllt seinen Zweck erst in einer passenden Schaltung; kein eigenständiges Gerät."
        dependencies = ["Passende Schaltung, Beschaltung und Integrationsprojekt"]
        if re.search(r"palette|großposten|grossposten|konvolut|\b[1-9]\d{2,}\b", title):
            scalp, band, confidence = 25, "unknown", "low"
    elif re.search(COMPONENT, title):
        kind, scalp, useability, band, confidence = "component", 58, 22, "unknown", "low"
        reason = "Technische Systembaugruppe mit möglicher substanzieller Bedeutung; Modell, Umfang und Nachfrage sind ungeklärt."
        use_reason = "Die Baugruppe benötigt externe Komponenten, Verdrahtung und eine passende Anwendung; sie erledigt allein noch keine vollständige Aufgabe."
        dependencies = ["Passende externe Komponenten", "Versorgung und Verdrahtung", "Konkrete Integrationsaufgabe"]
        if re.search(r"\brma\s*42\b", title):
            use_reason = "Universeller Sensortransmitter: benötigt externe Sensoren, Verdrahtung und eine passende Messaufgabe; kein eigenständiges Messsystem."
            dependencies = ["Externe Sensoren", "Passende Versorgung und Verdrahtung", "Mess- oder Steueranwendung"]
    elif re.search(DEVICE, title):
        kind, scalp, useability, band, confidence = "apparatus", 78, 90, "around_500", "low"
        reason = "Der Titel deutet auf ein eigenständiges Spezialgerät hin; grobe Geräteklasse, kein belegter Gebrauchtwert."
        use_reason = "Der Gerätetyp hat einen eigenen Anwendungszweck und benötigt voraussichtlich keine übergeordnete Maschine; konkrete Anschlüsse sind offen."
        if "autoklav" in title or re.search(r"schweißgerät|schweissgerät", title):
            useability = 80
            use_reason = "Eigenständiger Gerätetyp, jedoch mit üblichen Betriebsmitteln und modellabhängigen Anschlüssen; keine passende übergeordnete Maschine nötig."
            dependencies = ["Betriebsmittel und Anschlüsse nach Modellvorgaben"]
    elif re.search(TOOL, title):
        kind, scalp, useability, band, confidence = "tool", 50, 90, "around_50", "low"
        reason = "Nennenswertes Werkzeug denkbar; Modell, Qualität und Umfang können die Größenordnung stark ändern."
        use_reason = "Werkzeugtyp mit eigenem unmittelbarem Anwendungszweck; konkrete Versorgung und Lieferumfang sind noch zu klären."
        if "akku" in title:
            useability = 75
            dependencies = ["Kompatibler Akku und Ladegerät, falls nicht enthalten"]
            use_reason = "Das Werkzeug hat einen eigenen Anwendungszweck, braucht aber einen passenden Akku und eine Lademöglichkeit."
    elif re.search(MATERIAL, title):
        kind, scalp, useability, band, confidence = "consumable", 40, 45, "unknown", "low"
        reason = "Nutzbares Material oder Bauprofil; Menge, Ausführung und passender Bedarf entscheiden über das Interesse."
        use_reason = "Material wird erst mit einem passenden Bauprojekt, passenden Maßen und Befestigung nutzbar; keine seltene Spezialmaschine erforderlich."
        dependencies = ["Konkretes Bauprojekt", "Passende Maße und Befestigung"]
    else:
        kind, scalp, useability, band, confidence = "unknown", 35, 50, "unknown", "low"
        reason = "Aus dem Titel ist die Geräte- und Wertklasse nicht zuverlässig erkennbar; für semantische Sichtung offenhalten."
        use_reason = "Eigenständigkeit und benötigtes Umfeld sind unbekannt; der mittlere Heuristikwert ist keine geprüfte Nutzbarkeitsaussage."
        dependencies = ["Benötigtes Umfeld und Lieferumfang noch unbekannt"]
    risks = []
    text = title + " " + str(item.get("condition", "")).casefold()
    if re.search(DAMAGE, text):
        confidence = "low"
        risks.append("Defekt, ungeprüft oder unvollständig angegeben; keine Funktion unterstellen")
    structural_text = title + " " + str(item.get("description", "")).casefold()
    if kind in {"apparatus", "tool"}:
        missing = re.search(r"\bohne\s+(?:den\s+|die\s+|das\s+)?(motor|antrieb|steuerung|steuerelektronik|messkopf|aggregat)\b", structural_text)
        if missing:
            useability = 22
            use_reason = "Eine wesentliche Baugruppe fehlt ausdrücklich; erst deren Beschaffung und Integration ergibt ein vollständiges Gerät."
            dependencies = ["Fehlende Baugruppe: " + missing.group(1), "Kompatible Ergänzung und Integration"]
            risks.append("Ausdrücklich fehlende Baugruppe; Vollständigkeit und Ergänzbarkeit prüfen")
        elif re.search(r"\bohne\s+(?:das\s+)?netzteil\b", structural_text):
            useability = min(useability, 70)
            use_reason = "Eigener Anwendungszweck, aber eine passende externe Stromversorgung fehlt; deren Verfügbarkeit und Anschlüsse sind zu klären."
            dependencies = list(dict.fromkeys(dependencies + ["Passendes externes Netzteil"]))[:5]
    if "autoklav" in title:
        risks.append("Zustands-, Wartungs- und Prüfunterlagen klären; Verkäuferangabe ist keine Funktions- oder Sicherheitsprüfung")
    return {"product_kind": kind, "scalp_score": scalp, "useability_score": useability,
            "useability_reason": use_reason, "dependencies": dependencies, "value_band": band,
            "assessment_confidence": confidence, "value_reason": reason,
            "reasons": [reason], "risks": risks,
            "next_checks": ["Genaue Modellbezeichnung, Lieferumfang und Zustand im Originalangebot prüfen"]}


def _legacy_score(semantic_interest: int, fact_points: int, eligibility: str, kind: str) -> int:
    """Reconstruct a V1 review's old total only; never score a V2 candidate."""
    score = min(100, max(0, round(semantic_interest * 0.7 + fact_points)))
    if kind in {"media", "consumable"}:
        score = min(score, 25)
    if eligibility == "excluded":
        score = min(score, 15)
    return score


def _score(scalp_score: int, useability_score: int) -> int:
    """Independent dimensions combined with exact integer half-up rounding."""
    return (scalp_score * useability_score + 50) // 100


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
    time_failed: list[str] = []
    time_unknown: list[str] = []
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
        unknown.append("Beobachteter Auktionspreis unbekannt")
    elif price > float(settings.get("max_price_eur", 10)):
        failed.append("Beobachteter Preis über dem Suchlimit")
    else:
        points += 12 if price <= 1 else 7
        reasons.append("Beobachteter Auktionspreis höchstens 1 €" if price <= 1 else "Beobachteter Auktionspreis unter dem Suchlimit")
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
        time_unknown.append("Ablaufdatum unbekannt")
    elif end <= at:
        time_failed.append("Angebot abgelaufen")
    elif end >= at + timedelta(hours=float(settings.get("lookahead_hours", 72))):
        time_failed.append("Ablauf außerhalb des Suchfensters")
    seen = parse_time(item.get("last_seen_at"))
    if not seen or seen > at:
        unknown.append("Quellenstand unbekannt oder unplausibel")
    elif at - seen > timedelta(hours=2):
        unknown.append("Älterer Quellenstand; Preis, Gebote und Verfügbarkeit erneut prüfen")
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
    # Static clients may see an auction cross the lookahead boundary before
    # the next build. Preserve all non-time constraints explicitly so they can
    # recompute that boundary without weakening price/bid/shipping exclusions.
    eligibility_without_time_window = "excluded" if failed else ("review" if unknown else "eligible")
    failed += time_failed
    unknown += time_unknown
    eligibility = "excluded" if failed else ("review" if unknown else "eligible")
    assessment = heuristic_assessment(item)
    # Keep the historical fact tally only for audit and explicit V1 review
    # migration. It never contributes to either V2 factor or derived Interest.
    fact_points = max(0, points - (3 if unknown else 0))
    result.update(assessment)
    result.update({
        "eligibility": eligibility, "eligibility_reasons": failed + unknown,
        "eligibility_without_time_window": eligibility_without_time_window,
        "semantic_assessed": True, "assessment_basis": "heuristic",
        "fact_score": fact_points, "local_pickup_confirmed": local,
        "fact_reasons": _unique(reasons), "fact_risks": _unique(failed + unknown + risks),
        "reasons": _unique(reasons + assessment["reasons"]),
        "risks": _unique(failed + unknown + risks + assessment["risks"]),
        "total_known_eur": round(price + shipping, 2) if price is not None and shipping is not None else None,
    })
    result["score_version"] = 2
    result["semantic_interest"] = assessment["scalp_score"]  # Deprecated alias.
    result["score"] = result["interest_score"] = _score(assessment["scalp_score"], assessment["useability_score"])
    return apply_interest_profile(result)


def apply_semantic(item: dict[str, Any], assessment: dict[str, Any], *, score_version: int = 2) -> dict[str, Any]:
    """Model output can annotate/reorder a candidate, never change source facts."""
    if type(score_version) is not int or score_version not in {1, 2}:
        raise ValueError("Invalid score version")
    scalp = assessment.get("scalp_score" if score_version == 2 else "semantic_interest")
    if type(scalp) is not int or not 0 <= scalp <= 100:
        raise ValueError("Invalid Scalp score")
    if score_version == 2:
        useability = assessment.get("useability_score")
        if type(useability) is not int or not 0 <= useability <= 100:
            raise ValueError("Invalid Useability score")
        use_reason = assessment.get("useability_reason")
        dependencies = assessment.get("dependencies")
        if not isinstance(use_reason, str) or not use_reason.strip() or len(use_reason) > 500:
            raise ValueError("Invalid Useability reason")
        if (not isinstance(dependencies, list) or len(dependencies) > 5
                or any(not isinstance(value, str) or not value.strip() or len(value) > 240 for value in dependencies)):
            raise ValueError("Invalid dependencies")
    else:
        useability, use_reason, dependencies = None, LEGACY_USEABILITY_REASON, []
    band = assessment.get("value_band")
    confidence = assessment.get("assessment_confidence")
    kind = assessment.get("product_kind", "unknown")
    if band not in {"around_5", "around_50", "around_500", "unknown"}:
        raise ValueError("Invalid value band")
    if confidence not in {"low", "medium", "high"}:
        raise ValueError("Invalid confidence")
    if kind not in {"apparatus", "tool", "component", "accessory", "media", "consumable", "unknown"}:
        kind = "unknown"
    if score_version == 1:
        scalp = _legacy_score(scalp, int(item.get("fact_score", 0)), item.get("eligibility", "review"), kind)
    result = dict(item)
    result.update({"score_version": score_version, "scalp_score": scalp, "semantic_interest": scalp,
                   "useability_score": useability, "useability_reason": plain_text(use_reason, 500),
                   "dependencies": [plain_text(value, 240) for value in dependencies], "value_band": band,
                   "assessment_confidence": confidence, "product_kind": kind,
                   "value_reason": plain_text(assessment.get("value_reason"), 500),
                   "semantic_assessed": True, "assessment_basis": "llm"})
    for field in ("reasons", "risks", "next_checks"):
        values = assessment.get(field)
        values = [plain_text(v, 300) for v in values if isinstance(v, str)] if isinstance(values, list) else []
        # New semantic evidence replaces the old heuristic, not source facts.
        factual = item.get("fact_" + field, []) if field in {"reasons", "risks"} else []
        result[field] = _unique(list(factual) + values)
    result["score"] = result["interest_score"] = _score(scalp, useability) if score_version == 2 else None
    return apply_interest_profile(result)
