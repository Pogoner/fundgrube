"""Small, conservative boundary between untrusted source data and public facts."""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            return None
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def iso_time(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("A timezone-aware timestamp is required")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def euro(value: Any, *, explicit_eur: bool = False) -> float | None:
    if isinstance(value, dict):
        if value.get("currency") != "EUR":
            return None
        value = value.get("value")
    elif not explicit_eur:
        return None
    number = finite_number(value)
    return round(number, 2) if number is not None else None


class _PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in {"script", "style", "iframe", "noscript", "object"}:
            self.hidden += 1
        elif tag in {"br", "p", "li", "div", "tr", "h1", "h2", "h3"} and not self.hidden:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "iframe", "noscript", "object"}:
            self.hidden = max(0, self.hidden - 1)
        elif tag in {"p", "li", "div", "tr"} and not self.hidden:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def plain_text(value: Any, limit: int = 6000) -> str:
    if not isinstance(value, str):
        return ""
    parser = _PlainText()
    try:
        parser.feed(value[:100_000])
        parser.close()
    except (ValueError, RecursionError):
        return ""
    text = " ".join(" ".join(parser.parts).split())
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    text = re.sub(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b", "[E-Mail entfernt]", text)
    text = re.sub(r"(?i)(?:telefon|tel\.|mobil|whatsapp)\s*:?\s*[+\d][\d\s()/.-]{6,}", "[Kontaktdaten entfernt]", text)
    return text[:limit]


def safe_url(value: Any, *, image: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 2500:
        return ""
    try:
        parts = urlsplit(value)
        if parts.scheme != "https" or parts.username or parts.password or parts.port not in (None, 443):
            return ""
        host = parts.hostname or ""
        domains = ("ebayimg.com",) if image else ("ebay.de", "ebay.com")
        if not any(host == domain or host.endswith("." + domain) for domain in domains):
            return ""
        return value
    except (ValueError, TypeError):
        return ""


def normalize_item(raw: dict[str, Any], at: datetime, *, local_pickup_confirmed: bool = False,
                   pickup_radius_km: int = 50) -> dict[str, Any] | None:
    """No missing price, bid, currency, shipping or reserve field becomes zero/false."""
    if not isinstance(raw, dict):
        return None
    item_id = raw.get("itemId", raw.get("id"))
    if not isinstance(item_id, str) or not item_id.strip() or len(item_id) > 200:
        return None
    options = raw.get("buyingOptions", raw.get("buying_options", []))
    options = [s for s in options if isinstance(s, str)] if isinstance(options, list) else []
    is_auction = True if "AUCTION" in options else (False if options else None)
    if not options and isinstance(raw.get("is_auction"), bool):
        is_auction = raw["is_auction"]
    bids_value = raw.get("bidCount", raw.get("bid_count"))
    bids_number = finite_number(bids_value)
    bids = int(bids_number) if bids_number is not None and bids_number.is_integer() else None
    current = euro(raw.get("currentBidPrice"))
    minimum = euro(raw.get("minimumPriceToBid"))
    price, price_kind = None, "unknown"
    if current is not None and (bids != 0 or current > 0):
        price, price_kind = current, "current_bid"
    elif minimum is not None:
        price, price_kind = minimum, "minimum_bid"
    elif "price_eur" in raw:
        price = euro(raw["price_eur"], explicit_eur=True)
        price_kind = "current_bid" if price is not None else "unknown"
    elif is_auction and "FIXED_PRICE" not in options:
        price = euro(raw.get("price"))
        price_kind = "auction_price" if price is not None else "unknown"
    shipping = []
    raw_shipping = raw.get("shippingOptions")
    for option in raw_shipping if isinstance(raw_shipping, list) else []:
        if isinstance(option, dict):
            cost = euro(option.get("shippingCost"))
            if cost is not None:
                shipping.append(cost)
    if not shipping and "shipping_eur" in raw:
        cost = euro(raw["shipping_eur"], explicit_eur=True)
        if cost is not None:
            shipping.append(cost)
    reserve = "unknown"
    if raw.get("reservePriceMet") is False:
        reserve = "not_met"
    elif raw.get("reservePriceMet") is True:
        reserve = "met"
    elif raw.get("reserve_status") in {"none", "not_met", "met", "unknown"}:
        # Only an explicit licensed JSON field may certify no reserve.
        reserve = raw["reserve_status"]
    location = raw.get("itemLocation")
    location = location if isinstance(location, dict) else {}
    image = raw.get("image")
    image = image.get("imageUrl", "") if isinstance(image, dict) else image
    ending = parse_time(raw.get("itemEndDate", raw.get("ends_at")))
    description = raw.get("description") or raw.get("shortDescription") or ""
    return {
        "id": item_id.strip(),
        "title": plain_text(raw.get("title"), 300) or "Ohne Titel",
        "description": plain_text(description),
        "url": safe_url(raw.get("itemWebUrl", raw.get("url"))),
        "image": safe_url(image or raw.get("image_url"), image=True),
        "price_eur": price,
        "price_kind": price_kind,
        "shipping_eur": min(shipping) if shipping else None,
        "bid_count": bids,
        "ends_at": iso_time(ending) if ending else None,
        "last_seen_at": iso_time(at),
        "content_updated_at": iso_time(at),
        "city": plain_text(location.get("city", raw.get("city")), 120),
        "postcode": plain_text(location.get("postalCode", raw.get("postcode")), 20),
        "country": plain_text(location.get("country", raw.get("country")), 2),
        "distance_km": finite_number(raw.get("distance_km")),
        "condition": plain_text(raw.get("condition"), 160),
        "buying_options": options,
        "is_auction": is_auction,
        "reserve_status": reserve,
        "local_pickup_confirmed": local_pickup_confirmed is True or raw.get("local_pickup_confirmed") is True,
        "pickup_radius_km": pickup_radius_km,
        "details_fetched": raw.get("_details_fetched") is True,
    }
