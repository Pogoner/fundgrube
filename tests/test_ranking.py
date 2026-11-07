import copy
import unittest
from datetime import datetime, timedelta, timezone

from hunter.models import euro, iso_time, normalize_item, parse_time, plain_text, safe_url
from hunter.ranking import apply_semantic, assess_item


NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
SETTINGS = {"max_price_eur": 10, "max_shipping_eur": 15, "max_bids": 0,
            "pickup_radius_km": 50, "lookahead_hours": 24, "max_freshness_hours": 6}


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

    def test_stale_is_excluded(self):
        self.assertEqual(self.rank(raw(itemEndDate=iso_time(NOW + timedelta(hours=20))), NOW + timedelta(hours=7))["eligibility"], "excluded")

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


if __name__ == "__main__":
    unittest.main()
