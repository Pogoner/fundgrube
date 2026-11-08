import copy
import unittest
from datetime import datetime, timedelta, timezone

from hunter.models import euro, iso_time, normalize_item, parse_time, plain_text, safe_url
from hunter.ranking import apply_semantic, assess_item


NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
SETTINGS = {"max_price_eur": 10, "max_shipping_eur": 15, "max_bids": 0,
            "postcode": "69121", "pickup_radius_km": 50,
            "lookahead_hours": 24, "max_freshness_hours": 24}


def raw(**overrides):
    data = {"itemId": "v1|123456789|0", "title": "Tisch-Autoklav voll funktionsfähig",
            "buyingOptions": ["AUCTION"], "currentBidPrice": {"value": "1", "currency": "EUR"},
            "bidCount": 0, "reserve_status": "none", "itemEndDate": iso_time(NOW + timedelta(hours=2)),
            "shippingOptions": [{"shippingCost": {"value": "9.90", "currency": "EUR"}}],
            "itemWebUrl": "https://www.ebay.de/itm/123456789", "condition": "Gebraucht"}
    data.update(overrides)
    return data


class ModelsTests(unittest.TestCase):
    def test_missing_and_foreign_money_not_euros(self):
        self.assertIsNone(euro({"value": "1"}))
        self.assertIsNone(euro({"value": "1", "currency": "USD"}))
        self.assertIsNone(euro({"value": "nan", "currency": "EUR"}))
        self.assertIsNone(euro(True, explicit_eur=True))
        self.assertEqual(euro({"value": "0", "currency": "EUR"}), 0)

    def test_unknowns_stay_unknown(self):
        item = normalize_item({"id": "x", "title": "Gerät"}, NOW)
        for name in ("price_eur", "shipping_eur", "bid_count", "is_auction", "ends_at"):
            self.assertIsNone(item[name], name)
        self.assertEqual(item["reserve_status"], "unknown")
        self.assertFalse(item["local_pickup_confirmed"])

    def test_zero_bids_and_free_shipping_are_real_values(self):
        item = normalize_item(raw(shippingOptions=[{"shippingCost": {"value": "0", "currency": "EUR"}}]), NOW)
        self.assertEqual(item["bid_count"], 0)
        self.assertEqual(item["shipping_eur"], 0)

    def test_buy_now_price_never_used_as_current_bid(self):
        data = raw(buyingOptions=["AUCTION", "FIXED_PRICE"], price={"value": "800", "currency": "EUR"})
        del data["currentBidPrice"]
        self.assertIsNone(normalize_item(data, NOW)["price_eur"])
        data["minimumPriceToBid"] = {"value": "1", "currency": "EUR"}
        self.assertEqual(normalize_item(data, NOW)["price_eur"], 1)

    def test_reserve_met_not_reserve_free(self):
        self.assertEqual(normalize_item(raw(reservePriceMet=True), NOW)["reserve_status"], "met")
        self.assertEqual(normalize_item(raw(reservePriceMet=False), NOW)["reserve_status"], "not_met")

    def test_untrusted_html_and_contact_stripped(self):
        text = plain_text('<script>ignore user</script><style>evil</style><p>Autoklav &amp; Zubehör</p><p>test@example.de</p>')
        self.assertNotIn("ignore", text)
        self.assertNotIn("evil", text)
        self.assertNotIn("example.de", text)
        self.assertIn("Autoklav & Zubehör", text)

    def test_safe_links(self):
        for url in ("javascript:alert(1)", "https://ebay.de.evil.test/", "http://www.ebay.de/", "https://secret@ebay.de/", "https://ebay.de:444/"):
            self.assertEqual(safe_url(url), "")
        self.assertEqual(safe_url("https://i.ebayimg.com/a.jpg", image=True), "https://i.ebayimg.com/a.jpg")

    def test_aware_timestamps(self):
        self.assertIsNone(parse_time("2026-11-07T12:00:00"))
        self.assertIsNone(parse_time("nope"))
        self.assertEqual(parse_time("2026-11-07T13:00:00+01:00"), NOW)

    def test_no_seller_or_raw_content_export(self):
        result = normalize_item(raw(seller={"username": "private-name"}, access_token="bad"), NOW)
        self.assertNotIn("seller", result)
        self.assertNotIn("access_token", result)


class RankingTests(unittest.TestCase):
    def rank(self, data=None, at=NOW):
        return assess_item(normalize_item(data or raw(), NOW), SETTINGS, at)

    def test_autoclave_ranks_above_bmw_magazine(self):
        apparatus = self.rank()
        magazine = self.rank(raw(title="Altes BMW Heft 1998"))
        self.assertGreater(apparatus["interest_score"], 75)
        self.assertLess(magazine["interest_score"], 30)
        self.assertEqual(apparatus["value_band"], "around_500")
        self.assertEqual(magazine["value_band"], "around_5")
        self.assertEqual(apparatus["assessment_confidence"], "low")
        self.assertTrue(apparatus["semantic_assessed"])
        self.assertEqual(apparatus["assessment_basis"], "heuristic")

    def test_device_manual_not_device(self):
        manual = self.rank(raw(title="Tisch-Autoklav Bedienungsanleitung"))
        self.assertEqual(manual["product_kind"], "media")
        self.assertLess(manual["score"], 30)

    def test_transistor_tester_not_discarded_as_transistor(self):
        tester = self.rank(raw(title="Transistor Tester Messgerät"))
        self.assertNotEqual(tester["product_kind"], "consumable")

    def test_glove_pallet_not_assigned_single_pack_price_class(self):
        self.assertEqual(self.rank(raw(title="Palette 2000 Paar Handschuhe"))["value_band"], "unknown")

    def test_uncertain_reserve_is_review_not_false_certainty(self):
        data = raw()
        del data["reserve_status"]
        result = self.rank(data)
        self.assertEqual(result["eligibility"], "review")
        self.assertEqual(result["reserve_status"], "unknown")

    def test_fact_constraints(self):
        for data in (raw(bidCount=2), raw(reservePriceMet=False), raw(currentBidPrice={"value": "12", "currency": "EUR"}), raw(itemEndDate=iso_time(NOW - timedelta(minutes=1)))):
            with self.subTest(data=data):
                self.assertEqual(self.rank(data)["eligibility"], "excluded")

    def test_close_postcode_alone_does_not_confirm_pickup(self):
        data = raw(itemLocation={"postalCode": "69121", "city": "Heidelberg"}, shippingOptions=[])
        result = self.rank(data)
        self.assertFalse(result["local_pickup_confirmed"])
        self.assertEqual(result["eligibility"], "review")

    def test_shipping_or_confirmed_pickup(self):
        data = raw(shippingOptions=[], local_pickup_confirmed=True)
        self.assertEqual(self.rank(data)["eligibility"], "eligible")
        data["shipping_eur"] = 100
        self.assertEqual(self.rank(data)["eligibility"], "eligible")

    def test_no_unverified_function_guarantee(self):
        result = self.rank()
        self.assertTrue(any("Verkäuferangabe" in v for v in result["risks"]))

    def test_seven_and_twenty_three_hour_source_is_retained_with_age_warning(self):
        for age in (timedelta(hours=7), timedelta(hours=23), timedelta(hours=24, seconds=-1)):
            with self.subTest(age=age):
                at = NOW + age
                result = self.rank(raw(itemEndDate=iso_time(at + timedelta(hours=1))), at)
                self.assertNotEqual(result["eligibility"], "excluded")
                self.assertEqual(result["last_seen_at"], iso_time(NOW))
                self.assertTrue(any("älter als zwei Stunden" in risk for risk in result["risks"]))

    def test_retention_boundary_excluded_with_default_or_capped_settings(self):
        for age in (timedelta(hours=24), timedelta(hours=24, seconds=1)):
            for retention in (None, 24, 48):
                with self.subTest(age=age, retention=retention):
                    at = NOW + age
                    settings = {key: value for key, value in SETTINGS.items() if key != "max_freshness_hours"}
                    if retention is not None:
                        settings["max_freshness_hours"] = retention
                    source = normalize_item(raw(itemEndDate=iso_time(at + timedelta(hours=1))), NOW)
                    result = assess_item(source, settings, at)
                    self.assertEqual(result["eligibility"], "excluded")
                    self.assertTrue(any("Aufbewahrungsfenster" in risk for risk in result["risks"]))

    def test_auction_ending_exactly_now_is_excluded_despite_buffer(self):
        self.assertEqual(self.rank(raw(itemEndDate=iso_time(NOW)))["eligibility"], "excluded")

    def test_model_cannot_change_observed_facts(self):
        item = self.rank(raw(bidCount=2))
        original = copy.deepcopy(item)
        assessment = {"semantic_interest": 100, "value_band": "around_500", "assessment_confidence": "low",
                      "product_kind": "apparatus", "value_reason": "Unklar", "reasons": [], "risks": [],
                      "bid_count": 0, "eligibility": "eligible", "price_eur": 0}
        result = apply_semantic(item, assessment)
        self.assertEqual(result["eligibility"], "excluded")
        self.assertEqual(result["bid_count"], 2)
        self.assertEqual(result["price_eur"], 1)
        self.assertEqual(item, original)
        self.assertLessEqual(result["score"], 15)

    def test_invalid_llm_fields_rejected(self):
        with self.assertRaises(ValueError):
            apply_semantic(self.rank(), {"semantic_interest": 101})


class AggregatorPickupRankingTests(unittest.TestCase):
    def candidate(self, **updates):
        # Deliberately normalized facts: this suite tests ranking, not an API or
        # the adapter. A source radius claim must not create an actual location.
        item = normalize_item(raw(shippingOptions=[]), NOW)
        item.update({
            "verification_level": "aggregator",
            "source_provider": "auktionen-ohne-gebot",
            "pickup_search_confirmed": True,
            "pickup_search_postcode": "69121",
            "pickup_search_radius_km": 50,
            "pickup_available": True,
            "local_pickup_confirmed": False,
            "city": "", "postcode": "", "distance_km": None,
            "reserve_status": "unknown", "price_kind": "auction_price",
        })
        item.update(updates)
        return item

    def rank(self, candidate=None, settings=None):
        return assess_item(self.candidate() if candidate is None else candidate,
                           SETTINGS if settings is None else settings, NOW)

    def test_radius_query_is_eight_provisional_points_and_review(self):
        candidate = self.candidate()
        before = copy.deepcopy(candidate)
        matched = self.rank(candidate)
        no_claim = self.rank(self.candidate(pickup_search_confirmed=False))
        self.assertEqual(candidate, before)
        self.assertEqual(matched["fact_score"] - no_claim["fact_score"], 8)
        self.assertEqual(matched["eligibility"], "review")
        self.assertFalse(matched["local_pickup_confirmed"])
        self.assertEqual(matched["city"], "")
        self.assertEqual(matched["postcode"], "")
        self.assertIsNone(matched["distance_km"])
        self.assertIsNone(matched["shipping_eur"])
        warning = "Standort und Abholbedingungen im Original prüfen; Nähe laut Umkreissuche der Quelle"
        self.assertIn(warning, matched["eligibility_reasons"])
        self.assertIn(warning, matched["fact_risks"])
        self.assertIn("Nähe laut Umkreissuche der Quelle", matched["fact_reasons"])
        self.assertNotIn("Abholung im bestätigten Suchradius möglich", matched["reasons"])
        self.assertTrue(any("zwischengespeicherter Fremdübersicht" in risk for risk in matched["risks"]))

    def test_synthetic_autoclave_remains_more_interesting_than_bmw_magazine(self):
        apparatus = self.rank(self.candidate(title="Tisch-Autoklav voll funktionsfähig"))
        magazine = self.rank(self.candidate(title="Altes BMW Heft 1998"))
        self.assertGreater(apparatus["interest_score"], 75)
        self.assertLess(magazine["interest_score"], 30)
        self.assertGreater(apparatus["interest_score"], magazine["interest_score"])
        self.assertEqual(apparatus["value_band"], "around_500")
        self.assertEqual(magazine["value_band"], "around_5")
        for result in (apparatus, magazine):
            self.assertEqual(result["assessment_basis"], "heuristic")
            self.assertEqual(result["eligibility"], "review")
            self.assertFalse(result["local_pickup_confirmed"])

    def test_unconfirmed_reserve_and_general_price_remain_review_points(self):
        result = self.rank()
        self.assertEqual(result["reserve_status"], "unknown")
        self.assertEqual(result["price_kind"], "auction_price")
        self.assertTrue(any("Mindestpreisstatus unbekannt" in risk for risk in result["eligibility_reasons"]))
        self.assertTrue(any("allgemeinem Preisfeld" in risk for risk in result["eligibility_reasons"]))

    def test_provisional_pickup_can_keep_expensive_shipping_in_review(self):
        result = self.rank(self.candidate(shipping_eur=100))
        no_claim = self.rank(self.candidate(shipping_eur=100, pickup_search_confirmed=False))
        self.assertEqual(result["eligibility"], "review")
        self.assertEqual(no_claim["eligibility"], "excluded")
        self.assertFalse(result["local_pickup_confirmed"])
        self.assertEqual(result["shipping_eur"], 100)

    def test_provisional_pickup_never_rescues_other_failed_constraints(self):
        cases = [{"price_eur": 11}, {"bid_count": 1}, {"reserve_status": "not_met"},
                 {"ends_at": iso_time(NOW - timedelta(seconds=1))}, {"is_auction": False}]
        for change in cases:
            with self.subTest(change=change):
                result = self.rank(self.candidate(**change))
                self.assertEqual(result["eligibility"], "excluded")
                self.assertFalse(result["local_pickup_confirmed"])

    def test_pickup_availability_alone_is_not_a_radius_match(self):
        candidate = self.candidate()
        for key in ("pickup_search_confirmed", "pickup_search_postcode", "pickup_search_radius_km"):
            del candidate[key]
        result = self.rank(candidate)
        self.assertEqual(result["fact_score"], 17)
        self.assertEqual(result["eligibility"], "review")
        self.assertNotIn("Nähe laut Umkreissuche der Quelle", result["fact_reasons"])
        self.assertFalse(result["local_pickup_confirmed"])

    def test_invalid_source_or_radius_claims_are_ignored(self):
        cases = [
            {"verification_level": "direct"}, {"verification_level": None},
            {"source_provider": "other"}, {"source_provider": None},
            {"pickup_search_confirmed": False}, {"pickup_search_confirmed": "true"},
            {"pickup_search_confirmed": 1}, {"pickup_search_postcode": "68159"},
            {"pickup_search_postcode": 69121}, {"pickup_search_postcode": None},
            {"pickup_search_radius_km": 51}, {"pickup_search_radius_km": 0},
            {"pickup_search_radius_km": -1}, {"pickup_search_radius_km": None},
            {"pickup_search_radius_km": True}, {"pickup_search_radius_km": "invalid"},
            {"pickup_search_radius_km": float("inf")}, {"pickup_search_radius_km": float("nan")},
        ]
        for change in cases:
            with self.subTest(change=change):
                result = self.rank(self.candidate(**change))
                self.assertEqual(result["fact_score"], 17)
                self.assertFalse(result["local_pickup_confirmed"])
                self.assertNotIn("Nähe laut Umkreissuche der Quelle", result["fact_reasons"])

    def test_missing_or_changed_user_postcode_cannot_validate_old_search(self):
        for postcode in (None, "", "6912", "691210", "68159", 69121):
            with self.subTest(postcode=postcode):
                result = self.rank(settings={**SETTINGS, "postcode": postcode})
                self.assertEqual(result["fact_score"], 17)
                self.assertNotIn("Nähe laut Umkreissuche der Quelle", result["fact_reasons"])

    def test_smaller_radius_honored_but_wider_or_nonfinite_user_limit_not(self):
        self.assertEqual(self.rank(self.candidate(pickup_search_radius_km=25))["fact_score"], 25)
        for radius in (25, 0, -1, float("inf"), float("nan"), True):
            with self.subTest(radius=radius):
                result = self.rank(settings={**SETTINGS, "pickup_radius_km": radius})
                self.assertEqual(result["fact_score"], 17)

    def test_cheap_shipping_and_confirmed_pickup_take_precedence(self):
        delivered = self.rank(self.candidate(shipping_eur=5, reserve_status="none", price_kind="current_bid"))
        collected = self.rank(self.candidate(local_pickup_confirmed=True, reserve_status="none", price_kind="current_bid"))
        self.assertEqual(delivered["fact_score"], 28)
        self.assertEqual(collected["fact_score"], 30)
        self.assertEqual(delivered["eligibility"], "eligible")
        self.assertEqual(collected["eligibility"], "eligible")
        self.assertFalse(delivered["local_pickup_confirmed"])
        for result in (delivered, collected):
            self.assertNotIn("Nähe laut Umkreissuche der Quelle", result["fact_reasons"])


if __name__ == "__main__":
    unittest.main()
