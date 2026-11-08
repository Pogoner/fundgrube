import copy
import unittest
from datetime import datetime, timedelta, timezone

from hunter.models import euro, iso_time, normalize_item, parse_time, plain_text, safe_url
from hunter.ranking import apply_interest_profile, apply_semantic, assess_item


NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
SETTINGS = {"max_price_eur": 10, "max_shipping_eur": 15, "max_bids": 0,
            "postcode": "69121", "pickup_radius_km": 50,
            "lookahead_hours": 72, "description_max_age_hours": 24}


def raw(**overrides):
    data = {"itemId": "v1|123456789|0", "title": "Tisch-Autoklav voll funktionsfähig",
            "buyingOptions": ["AUCTION"], "currentBidPrice": {"value": "1", "currency": "EUR"},
            "bidCount": 0, "reserve_status": "none", "itemEndDate": iso_time(NOW + timedelta(hours=2)),
            "shippingOptions": [{"shippingCost": {"value": "9.90", "currency": "EUR"}}],
            "itemWebUrl": "https://www.ebay.de/itm/123456789", "condition": "Gebraucht"}
    data.update(overrides)
    return data


def assessment(**updates):
    result = {"scalp_score": 85, "useability_score": 90, "useability_reason": "Eigenständiges Gerät mit üblicher Versorgung.",
              "dependencies": [], "value_band": "around_500", "assessment_confidence": "low",
              "product_kind": "apparatus", "value_reason": "Spezialgerät mit plausiblem Potenzial.",
              "reasons": [], "risks": [], "next_checks": []}
    result.update(updates)
    return result


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
        self.assertGreater(apparatus["scalp_score"], 70)
        self.assertGreater(apparatus["interest_score"], 50)
        self.assertLess(magazine["interest_score"], 30)
        self.assertEqual(apparatus["value_band"], "around_500")
        self.assertEqual(magazine["value_band"], "around_5")
        self.assertEqual(apparatus["assessment_confidence"], "low")
        self.assertTrue(apparatus["semantic_assessed"])
        self.assertEqual(apparatus["assessment_basis"], "heuristic")
        self.assertEqual(apparatus["score_version"], 2)
        self.assertEqual(apparatus["semantic_interest"], apparatus["scalp_score"])

    def test_device_manual_not_device(self):
        manual = self.rank(raw(title="Tisch-Autoklav Bedienungsanleitung"))
        self.assertEqual(manual["product_kind"], "media")
        self.assertLess(manual["score"], 30)

    def test_explicit_glass_part_beats_referenced_instrument_keywords(self):
        for title in (
            "Glas - Teil E1 für Carl Zeiss Jena Refraktometer Mikroskop Objektiv Okular Tubus",
            "Glas - Teil E1 fuer Carl Zeiss Jena Refraktometer Mikroskop Objektiv Okular Tubus",
            "Teil E1 für Carl Zeiss Jena",
            "Ersatzteile für Mikroskop Carl Zeiss",
        ):
            with self.subTest(title=title):
                result = self.rank(raw(title=title))
                self.assertEqual(result["product_kind"], "component")
                self.assertLessEqual(result["useability_score"], 10)
                self.assertNotEqual(result["value_band"], "around_500")
                self.assertIn("ausdrücklich ein Teil", result["value_reason"])
                self.assertFalse(result["personal_interest_excluded"])
                self.assertEqual(result["assessment_basis"], "heuristic")

    def test_brochure_not_compressor_and_standalone_media_is_personally_excluded(self):
        for title in (
            "KAESER FAHRBARE BAUKOMPRESSOREN MOBILAIR M 27 / M 31 BROSCHÜRE 12 Seiten RAR",
            "Kaeser Mobilair M27 Broschuere 12 Seiten",
            "5 Broschüren und Prospekte zu Kompressoren",
            "Prospekte Carl Zeiss Jena Mikroskop",
            "Kataloge für Autoklaven",
            "Handbücher für Mikroskope",
            "Handbuecher fuer Mikroskope",
            "Betriebsanleitung Tisch-Autoklav",
            "Prospekt mit Bedienungsanleitung Kompressor",
        ):
            with self.subTest(title=title):
                result = self.rank(raw(title=title))
                self.assertEqual(result["product_kind"], "media")
                self.assertEqual(result["scalp_score"], 4)
                self.assertEqual(result["useability_score"], 95)
                self.assertEqual(result["interest_score"], 4)
                self.assertTrue(result["personal_interest_excluded"])
                self.assertIn("persönlichen Suchprofil", result["personal_interest_reason"])
                self.assertEqual(result["eligibility"], "eligible")
                self.assertEqual(result["assessment_basis"], "heuristic")

    def test_bundled_or_missing_manual_does_not_turn_a_tool_into_media(self):
        for title in (
            "Bohrmaschine mit Bedienungsanleitung", "Bohrmaschine inkl. Anleitung",
            "Bohrmaschine inklusive deutscher Betriebsanleitung",
            "Bohrmaschine mit Koffer und Bedienungsanleitung",
            "Bohrmaschine samt Handbuch", "Bohrmaschine + Bedienungsanleitung",
            "Bohrmaschine ohne Bedienungsanleitung", "Bohrmaschine Anleitung fehlt",
            "Bohrmaschine Anleitung nicht dabei", "Bohrmaschine mit Ersatzteilen für die Spindel",
        ):
            with self.subTest(title=title):
                result = self.rank(raw(title=title))
                self.assertEqual(result["product_kind"], "tool")
                self.assertGreaterEqual(result["useability_score"], 75)
                self.assertFalse(result["personal_interest_excluded"])
                self.assertIsNone(result["personal_interest_reason"])

    def test_physical_media_holders_are_not_paper(self):
        for title in (
            "Prospekthalter DIN A4", "Broschürenständer Metall", "Broschuerenstaender Metall",
            "Prospekt-Ständer DIN A4", "Prospekt Halter DIN A4", "Halter für Prospekte",
            "Ständer fuer Broschueren", "Zeitschriftenregal",
        ):
            with self.subTest(title=title):
                result = self.rank(raw(title=title))
                self.assertNotEqual(result["product_kind"], "media")
                self.assertFalse(result["personal_interest_excluded"])
                self.assertIsNone(result["personal_interest_reason"])

    def test_named_attachments_are_not_the_referenced_whole_machine(self):
        cases = (
            ("Drehmaschine Mazak QT 10 Quickturn Monitor - defekt", "component", 15),
            ("CNC Monitor für Mazak QT 10", "component", 15),
            ("SIP Mig Schweißgerät Spitzen und Düse für 0,8 mm Draht M5", "consumable", 35),
            ("MIG Schweissgeraet Kontaktspitzen und Duesen M5", "consumable", 35),
            ("Stichsägenblatt", "consumable", 40),
            ("Stichsägeblätter Bosch 10 Stück", "consumable", 40),
            ("Stichsaegeblaetter fuer Makita", "consumable", 40),
            ("Schnellspannbohrfutter für Bohrmaschine | Werkzeugzubehör", "accessory", 45),
            ("Bohrfutter fuer Bohrmaschine", "accessory", 45),
        )
        for title, kind, useability in cases:
            with self.subTest(title=title):
                result = self.rank(raw(title=title))
                self.assertEqual(result["product_kind"], kind)
                self.assertEqual(result["useability_score"], useability)
                self.assertLess(result["interest_score"], 20)
                self.assertEqual(result["value_band"], "unknown")
                self.assertFalse(result["personal_interest_excluded"])
                self.assertEqual(result["assessment_basis"], "heuristic")

    def test_bundled_attachments_do_not_reduce_whole_device_scores(self):
        cases = (
            ("Drehmaschine Mazak QT 10 Quickturn mit Monitor - defekt", "Drehmaschine Mazak QT 10 Quickturn - defekt"),
            ("Drehmaschine inklusive passendem Monitor", "Drehmaschine"),
            ("SIP Mig Schweißgerät mit Spitzen und Düse für 0,8 mm Draht M5", "SIP Mig Schweißgerät"),
            ("Schweißgerät mit Düse", "Schweißgerät"),
            ("Bohrmaschine mit Schnellspannbohrfutter", "Bohrmaschine"),
            ("Tischbohrmaschine mit Bohrfutter", "Tischbohrmaschine"),
            ("Stichsäge mit Stichsägeblättern", "Stichsäge"),
            ("Stichsäge inkl. Stichsägenblatt", "Stichsäge"),
        )
        for title, base in cases:
            with self.subTest(title=title):
                result, baseline = self.rank(raw(title=title)), self.rank(raw(title=base))
                for field in ("product_kind", "scalp_score", "useability_score", "interest_score"):
                    self.assertEqual(result[field], baseline[field], field)
                self.assertFalse(result["personal_interest_excluded"])

    def test_generic_monitor_is_not_invented_as_a_machine_part(self):
        result = self.rank(raw(title="Samsung Monitor 24 Zoll"))
        self.assertNotEqual(result["product_kind"], "component")

    def test_media_profile_cannot_be_overridden_by_high_or_wrong_semantic_scores(self):
        item = self.rank(raw(title="KAESER MOBILAIR Broschüre 12 Seiten RAR"))
        before = copy.deepcopy(item)
        for kind in ("media", "apparatus", "tool"):
            with self.subTest(kind=kind):
                result = apply_semantic(item, assessment(
                    scalp_score=99, useability_score=99, product_kind=kind,
                    personal_interest_excluded=False, personal_interest_reason=None))
                self.assertTrue(result["personal_interest_excluded"])
                self.assertEqual(result["scalp_score"], 99)
                self.assertEqual(result["useability_score"], 99)
                self.assertEqual(result["interest_score"], 98)
                self.assertEqual(result["assessment_basis"], "llm")
                self.assertEqual(result["product_kind"], kind)
                self.assertEqual(result["eligibility"], before["eligibility"])
                self.assertEqual(result["eligibility_reasons"], before["eligibility_reasons"])
                self.assertEqual(result["last_seen_at"], before["last_seen_at"])
                self.assertEqual(result["price_eur"], before["price_eur"])
        self.assertEqual(item, before)

    def test_profile_uses_semantic_media_kind_but_preserves_provenance_and_bundle_context(self):
        reviewed = {**self.rank(raw(title="KAESER Unterlagen alt")), "product_kind": "media",
                    "assessment_basis": "llm", "scalp_score": 22, "useability_score": 99,
                    "semantic_assessed_at": "2026-11-01T12:00:00Z", "semantic_review_mode": "chatgpt-interactive",
                    "personal_interest_excluded": False, "personal_interest_reason": "old"}
        original = copy.deepcopy(reviewed)
        result = apply_interest_profile(reviewed)
        self.assertTrue(result["personal_interest_excluded"])
        for field in set(reviewed) - {"personal_interest_excluded", "personal_interest_reason"}:
            self.assertEqual(result[field], reviewed[field], field)
        self.assertEqual(reviewed, original)
        for title in ("Bohrmaschine mit Bedienungsanleitung", "Prospekthalter", "Halter für Prospekte"):
            with self.subTest(title=title):
                result = apply_interest_profile({**reviewed, "title": title, "personal_interest_excluded": True})
                self.assertFalse(result["personal_interest_excluded"])
                self.assertIsNone(result["personal_interest_reason"])

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

    def test_source_age_never_excludes_an_ongoing_auction(self):
        for age in (timedelta(hours=24), timedelta(hours=72), timedelta(days=7)):
            for retention in (None, 0.5, 24, 48):
                with self.subTest(age=age, retention=retention):
                    at = NOW + age
                    settings = {key: value for key, value in SETTINGS.items() if key != "max_freshness_hours"}
                    if retention is not None:
                        settings["max_freshness_hours"] = retention
                    source = normalize_item(raw(itemEndDate=iso_time(at + timedelta(hours=1))), NOW)
                    result = assess_item(source, settings, at)
                    self.assertEqual(result["eligibility"], "review")
                    self.assertEqual(result["last_seen_at"], source["last_seen_at"])
                    self.assertTrue(any("älter als zwei Stunden" in risk for risk in result["risks"]))

    def test_remaining_time_window_is_strict_at_both_ends(self):
        cases = ((timedelta(seconds=-1), "excluded"), (timedelta(0), "excluded"),
                 (timedelta(seconds=1), "eligible"), (timedelta(hours=72, seconds=-1), "eligible"),
                 (timedelta(hours=72), "excluded"), (timedelta(hours=72, seconds=1), "excluded"))
        for remaining, eligibility in cases:
            with self.subTest(remaining=remaining):
                result = self.rank(raw(itemEndDate=iso_time(NOW + remaining)))
                self.assertEqual(result["eligibility"], eligibility)

    def test_default_window_and_configured_fraction_are_exclusive(self):
        for settings, remaining, expected in (({}, 48, "eligible"), ({}, 72, "excluded"),
                                              ({"lookahead_hours": 0.5}, 0.5, "excluded"),
                                              ({"lookahead_hours": 0.5}, 0.49, "eligible")):
            with self.subTest(settings=settings, remaining=remaining):
                item = normalize_item(raw(itemEndDate=iso_time(NOW + timedelta(hours=remaining))), NOW)
                self.assertEqual(assess_item(item, settings, NOW)["eligibility"], expected)

    def test_time_window_baseline_preserves_all_other_facts_and_uncertainty(self):
        beyond = iso_time(NOW + timedelta(hours=73))
        cases = ((raw(itemEndDate=beyond), "eligible"),
                 (raw(itemEndDate=beyond, reserve_status="unknown"), "review"),
                 (raw(itemEndDate=beyond, bidCount=1), "excluded"),
                 (raw(itemEndDate=beyond, currentBidPrice={"value": "11", "currency": "EUR"}), "excluded"),
                 (raw(itemEndDate=beyond, shippingOptions=[{"shippingCost": {"value": "100", "currency": "EUR"}}]), "excluded"),
                 (raw(itemEndDate=beyond, reserve_status="not_met"), "excluded"))
        for data, base in cases:
            with self.subTest(data=data):
                result = self.rank(data)
                self.assertEqual(result["eligibility"], "excluded")
                self.assertEqual(result["eligibility_without_time_window"], base)
                self.assertIn("Ablauf außerhalb des Suchfensters", result["eligibility_reasons"])

    def test_old_observation_remains_uncertain_in_time_independent_baseline(self):
        at = NOW + timedelta(days=5)
        result = self.rank(raw(itemEndDate=iso_time(at + timedelta(hours=73))), at)
        self.assertEqual(result["eligibility_without_time_window"], "review")
        self.assertEqual(result["eligibility"], "excluded")
        self.assertEqual(result["last_seen_at"], iso_time(NOW))

    def test_auction_ending_exactly_now_is_excluded_despite_buffer(self):
        self.assertEqual(self.rank(raw(itemEndDate=iso_time(NOW)))["eligibility"], "excluded")

    def test_model_cannot_change_observed_facts(self):
        item = self.rank(raw(bidCount=2))
        original = copy.deepcopy(item)
        answer = assessment(scalp_score=100, useability_score=100,
                            bid_count=0, eligibility="eligible", price_eur=0)
        result = apply_semantic(item, answer)
        self.assertEqual(result["eligibility"], "excluded")
        self.assertEqual(result["eligibility_without_time_window"], "excluded")
        self.assertEqual(result["bid_count"], 2)
        self.assertEqual(result["price_eur"], 1)
        self.assertEqual(item, original)
        self.assertEqual(result["score"], 100)  # Eligibility is separate, not a score cap.

    def test_invalid_llm_fields_rejected(self):
        for change in ({"scalp_score": 101}, {"scalp_score": True}, {"scalp_score": None},
                       {"useability_score": -1}, {"useability_score": 42.0}, {"useability_score": None},
                       {"useability_reason": ""}, {"useability_reason": "x" * 501},
                       {"dependencies": ["x"] * 6}, {"dependencies": ["x" * 241]}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                apply_semantic(self.rank(), assessment(**change))

    def test_interest_is_exact_half_up_product_without_fact_or_category_caps(self):
        for scalp, useability, expected in ((1, 50, 1), (3, 50, 2), (51, 50, 26), (85, 90, 77),
                                            (100, 100, 100), (0, 100, 0), (100, 0, 0), (90, 5, 5), (5, 95, 5)):
            for kind in ("apparatus", "media", "consumable", "component"):
                with self.subTest(scalp=scalp, useability=useability, kind=kind):
                    source = {**self.rank(raw(bidCount=2)), "fact_score": 0}
                    result = apply_semantic(source, assessment(scalp_score=scalp, useability_score=useability, product_kind=kind))
                    self.assertEqual(result["interest_score"], expected)
                    self.assertEqual(result["score"], expected)
                    self.assertEqual(result["scalp_score"], scalp)
                    self.assertEqual(result["semantic_interest"], scalp)
                    self.assertEqual(result["useability_score"], useability)
                    self.assertEqual(result["score_version"], 2)
                    self.assertEqual(result["eligibility"], "excluded")

    def test_heuristic_autonomy_does_not_treat_condition_as_missing_structure(self):
        baseline = self.rank(raw(title="Tisch-Autoklav"))
        for suffix in ("defekt", "ungeprüft", "Bastlergerät", "als Ersatzteil", "Ersatzteilträger", "als Ersatzteil für Bastler"):
            with self.subTest(suffix=suffix):
                result = self.rank(raw(title="Tisch-Autoklav " + suffix, condition="Als Ersatzteil / defekt"))
                self.assertEqual(result["product_kind"], "apparatus")
                self.assertEqual(result["useability_score"], baseline["useability_score"])
                self.assertEqual(result["scalp_score"], baseline["scalp_score"])
                self.assertEqual(result["assessment_basis"], "heuristic")
                self.assertTrue(any("keine Funktion unterstellen" in risk for risk in result["risks"]))

    def test_explicitly_missing_core_assembly_is_an_integration_dependency(self):
        complete = self.rank(raw(title="Tisch-Autoklav"))
        incomplete = self.rank(raw(title="Tisch-Autoklav ohne Steuerung"))
        self.assertLess(incomplete["useability_score"], complete["useability_score"])
        self.assertTrue(any("steuerung" in value for value in incomplete["dependencies"]))
        self.assertEqual(incomplete["scalp_score"], complete["scalp_score"])
        self.assertEqual(self.rank(raw(title="Tisch-Autoklav ohne Anleitung"))["useability_score"], complete["useability_score"])

    def test_rma42_is_universal_transmitter_but_not_a_standalone_system(self):
        result = self.rank(raw(title="RMA42 Universaltransmitter"))
        self.assertEqual(result["product_kind"], "component")
        self.assertLessEqual(result["useability_score"], 34)
        self.assertIn("Sensortransmitter", result["useability_reason"])
        self.assertIn("Externe Sensoren", result["dependencies"])
        self.assertNotIn("Motor", result["useability_reason"])
        self.assertEqual(result["assessment_basis"], "heuristic")

    def test_quantity_can_change_scalp_without_changing_consumer_autonomy(self):
        small = self.rank(raw(title="Packung Handschuhe"))
        pallet = self.rank(raw(title="Palette 2000 Paar Handschuhe"))
        self.assertGreater(pallet["scalp_score"], small["scalp_score"])
        self.assertEqual(pallet["useability_score"], small["useability_score"])
        self.assertGreater(pallet["interest_score"], 25)  # No old consumable cap.

    def test_legacy_review_migrates_old_total_without_inventing_useability(self):
        old = {key: value for key, value in assessment().items()
               if key not in {"scalp_score", "useability_score", "useability_reason", "dependencies"}}
        old["semantic_interest"] = 82
        for changes, expected in (({}, 85), ({"product_kind": "media"}, 25)):
            with self.subTest(changes=changes):
                result = apply_semantic(self.rank(), {**old, **changes}, score_version=1)
                self.assertEqual(result["scalp_score"], expected)  # round(82*.7+28), then V1 cap.
                self.assertEqual(result["semantic_interest"], expected)
                self.assertIsNone(result["useability_score"])
                self.assertIsNone(result["interest_score"])
                self.assertIsNone(result["score"])
                self.assertEqual(result["score_version"], 1)
                self.assertEqual(result["dependencies"], [])
                self.assertIn("noch nicht beurteilt", result["useability_reason"])
                self.assertEqual(result["assessment_basis"], "llm")
        excluded = apply_semantic(self.rank(raw(bidCount=2)), old, score_version=1)
        self.assertEqual(excluded["scalp_score"], 15)

    def test_legacy_shape_is_not_an_implicit_modern_assessment(self):
        with self.assertRaises(ValueError):
            apply_semantic(self.rank(), {"semantic_interest": 85})
        for version in (True, 0, 3, None):
            with self.subTest(version=version), self.assertRaises(ValueError):
                apply_semantic(self.rank(), assessment(), score_version=version)


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
        self.assertEqual(matched["interest_score"], no_claim["interest_score"])
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
        self.assertGreater(apparatus["scalp_score"], 70)
        self.assertGreater(apparatus["interest_score"], 50)
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
