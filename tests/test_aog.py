"""Hermetic tests for the robots-respecting public aggregator adapter."""

import json
import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

from hunter.aog import (
    API_PATH, MAX_JSON_BYTES, MAX_ROBOTS_BYTES, ORIGIN, PROVIDER, ROBOTS_URL,
    SOURCE_URL, USER_AGENT, HttpResponse, RobotsPolicy, _NoRedirect, collect,
)
from hunter.models import normalize_item
from hunter.pipeline import run_pipeline

NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
ROBOTS = "User-agent: *\nDisallow: /intern/\nAllow: /\n# KI willkommen\n"
SETTINGS = {"aog_max_requests_per_run": 2, "max_price_eur": 1, "postcode": "69121", "pickup_radius_km": 50}


def robots(body=ROBOTS, status=200, headers=None):
    return HttpResponse(status, {"Content-Type": "text/plain; charset=utf-8", **(headers or {})}, body.encode())


def article(number="336872529492", **updates):
    row = {"titel": "Labor-Messgerät", "bild": "https://i.ebayimg.com/example.jpg", "preis": 1,
           "versand": 1.9, "ende": "2026-11-07T22:08:22.000Z", "gebote": 0, "zustand": "Gebraucht",
           "link": f"https://www.ebay.de/itm/{number}?campid=5338945155&customid=aog-business-industrie&toolid=10049"}
    row.update(updates)
    return row


def api(rows=None, total=None, *, status=200, headers=None, payload=None):
    rows = [article()] if rows is None else rows
    payload = {"gesamt": len(rows) if total is None else total, "artikel": rows} if payload is None else payload
    return HttpResponse(status, {"Content-Type": "application/json", "Cache-Control": "public,max-age=900", **(headers or {})}, json.dumps(payload).encode())


class FakeTransport:
    def __init__(self, values):
        self.values, self.requests = list(values), []

    def __call__(self, **request):
        self.requests.append(request)
        if not self.values:
            raise AssertionError("No queued fake response")
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def run(values, *, settings=None, state=None, at=NOW):
    fake, delays = FakeTransport(values), []
    result = collect({**SETTINGS, **(settings or {})}, at, state, transport=fake, sleep=delays.append)
    return result, fake, delays


class RobotsTests(unittest.TestCase):
    def test_verified_generic_policy(self):
        policy = RobotsPolicy(ROBOTS)
        self.assertTrue(policy.allowed(ORIGIN + API_PATH + "?q=&kat=12576"))
        self.assertFalse(policy.allowed(ORIGIN + "/intern/secret"))

    def test_wildcard_and_terminal_anchor(self):
        policy = RobotsPolicy("User-agent: *\nDisallow: /api/*?*\nAllow: /api/auktionen?ok=1$\n")
        self.assertFalse(policy.allowed(ORIGIN + "/api/auktionen?x=1"))
        self.assertTrue(policy.allowed(ORIGIN + "/api/auktionen?ok=1"))
        self.assertFalse(policy.allowed(ORIGIN + "/api/auktionen?ok=1&x=2"))

    def test_longest_rule_and_allow_tie_not_first_match(self):
        for rules in ("Allow: /\nDisallow: /api\nAllow: /api/auktionen\n",
                      "Disallow: /api/auktionen\nAllow: /api/auktionen\n"):
            policy = RobotsPolicy("User-agent: *\n" + rules)
            self.assertTrue(policy.allowed(ORIGIN + API_PATH))

    def test_specific_user_agent_group_wins_over_star(self):
        policy = RobotsPolicy("User-agent: *\nAllow: /\nUser-agent: fundgrubebot\nDisallow: /api/\n")
        self.assertFalse(policy.allowed(ORIGIN + API_PATH))

    def test_same_named_groups_merge_and_case_is_ignored(self):
        policy = RobotsPolicy("User-agent: FUNDGRUBEBOT\nDisallow: /\nUser-agent: FundgrubeBot\nAllow: /api/\n")
        self.assertTrue(policy.allowed(ORIGIN + API_PATH))
        self.assertFalse(policy.allowed(ORIGIN + "/intern/"))

    def test_percent_encoded_unreserved_path_matches(self):
        policy = RobotsPolicy("User-agent: *\nDisallow: /%61pi/\n")
        self.assertFalse(policy.allowed(ORIGIN + API_PATH))

    def test_unrelated_bot_group_does_not_block_this_agent(self):
        policy = RobotsPolicy("User-agent: OtherBot\nDisallow: /\n")
        self.assertTrue(policy.allowed(ORIGIN + API_PATH))

    def test_crawl_delay_and_request_rate_honored(self):
        result, _, delays = run([robots("User-agent: *\nAllow: /\nCrawl-delay: 12\nRequest-rate: 1/15\n"), api(), api([])])
        self.assertEqual(delays, [15, 15])
        self.assertEqual(result["status"], "partial")

    def test_robots_disallow_means_no_api_requests(self):
        result, fake, _ = run([robots("User-agent: *\nDisallow: /api/*\n")])
        self.assertEqual(len(fake.requests), 1)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["warnings"][0]["code"], "robots_disallowed")

    def test_robots_html_challenge_and_missing_file_fail_closed(self):
        for response in (robots("<!doctype html><html>captcha</html>"), robots(status=404),
                         robots("not a robots file"), robots("Allow: /", headers={"Content-Type": "text/html"})):
            with self.subTest(response=response):
                result, fake, _ = run([response])
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["items"], [])
                self.assertEqual(len(fake.requests), 1)


class ParsingTests(unittest.TestCase):
    def test_raw_fields_affiliate_link_and_conservative_timestamp(self):
        raw = article()
        result, fake, _ = run([robots(), api([raw], headers={"Age": "120"}), api([])])
        row = result["items"][0]
        self.assertEqual(row["id"], "v1|336872529492|0")
        self.assertEqual(row["url"], raw["link"])
        self.assertEqual(row["source_provider"], PROVIDER)
        self.assertEqual(row["verification_level"], "aggregator")
        self.assertEqual(row["source_url"], SOURCE_URL)
        self.assertEqual(row["source_observed_at"], "2026-11-07T11:43:00Z")
        self.assertEqual(row["last_seen_at"], row["source_observed_at"])
        self.assertEqual(row["price_eur"], 1)
        self.assertEqual(row["shipping_eur"], 1.9)
        self.assertEqual(row["bid_count"], 0)
        self.assertEqual(row["price_kind"], "auction_price")
        self.assertEqual(row["description"], "")
        self.assertNotIn("city", row)
        self.assertNotIn("reserve_status", row)
        self.assertFalse(row["local_pickup_confirmed"])
        self.assertTrue(all(urlsplit(req["url"]).hostname == "auktionen-ohne-gebot.de" for req in fake.requests))

    def test_date_age_and_max_age_choose_conservative_oldest_bound(self):
        result, _, _ = run([robots(), api(headers={"Age": "10", "Date": "Sat, 07 Nov 2026 11:55:00 GMT", "Cache-Control": "max-age=1800"}), api([])])
        self.assertEqual(result["items"][0]["source_observed_at"], "2026-11-07T11:25:00Z")

    def test_unknown_shipping_bids_price_and_end_not_invented(self):
        raw = article(versand=None, gebote=None, preis=None, ende=None)
        result, _, _ = run([robots(), api([raw]), api([])])
        row = result["items"][0]
        for key in ("shipping_eur", "bid_count", "price_eur", "ends_at", "is_auction", "pickup_available"):
            self.assertIsNone(row[key])

    def test_missing_fields_remain_unknown(self):
        raw = {"titel": "Unbekannt", "link": article()["link"]}
        result, _, _ = run([robots(), api([raw]), api([])])
        self.assertIsNone(result["items"][0]["bid_count"])
        self.assertIsNone(result["items"][0]["shipping_eur"])

    def test_explicit_pickup_search_claim_without_invented_location(self):
        result, _, _ = run([robots(), api([]), api([article(abholung=True, versand=None)])])
        row = result["items"][0]
        self.assertTrue(row["pickup_search_confirmed"])
        self.assertEqual(row["pickup_search_postcode"], "69121")
        self.assertEqual(row["pickup_search_radius_km"], 50)
        self.assertFalse(row["local_pickup_confirmed"])
        self.assertNotIn("distance_km", row)
        self.assertEqual(result["coverage"]["pickup_search_count"], 1)

    def test_pickup_claim_needs_both_query_and_explicit_true(self):
        general = article(abholung=True)
        pickup = article("336872529493", abholung=None)
        result, _, _ = run([robots(), api([general]), api([pickup])])
        self.assertTrue(all(not row["pickup_search_confirmed"] for row in result["items"]))

    def test_pickup_dedup_preserves_oldest_observation_and_query_evidence(self):
        result, _, _ = run([robots(), api([article()], headers={"Age": "120"}), api([article(abholung=True, versand=None)])])
        self.assertEqual(len(result["items"]), 1)
        row = result["items"][0]
        self.assertTrue(row["pickup_search_confirmed"])
        self.assertEqual(row["last_seen_at"], "2026-11-07T11:43:00Z")

    def test_slug_item_link_parsed_offline(self):
        raw = article(link="https://www.ebay.de/itm/Laborgeraet/336872529492?campaign=original")
        result, _, _ = run([robots(), api([raw]), api([])])
        self.assertEqual(result["items"][0]["id"], "v1|336872529492|0")
        self.assertEqual(result["items"][0]["url"], raw["link"])

    def test_redirect_foreign_and_malformed_links_reject_whole_page(self):
        links = ("https://example.com/itm/336872529492", "https://www.ebay.de.evil.invalid/itm/336872529492",
                 "https://r.ebay.com/redirect", "https://www.ebay.de/itm/123", "https://secret@www.ebay.de/itm/336872529492")
        for link in links:
            with self.subTest(link=link):
                result, fake, _ = run([robots(), api([article(), article(link=link)])])
                self.assertEqual(result["items"], [])
                self.assertEqual(result["status"], "error")
                self.assertEqual(len(fake.requests), 2)

    def test_bad_numeric_or_optional_types_do_not_become_zero(self):
        for overrides in ({"gebote": -1}, {"gebote": True}, {"versand": "gratis"}, {"preis": float("nan")},
                          {"abholung": "true"}, {"ende": "morgen"}, {"zustand": {"html": "bad"}}):
            with self.subTest(overrides=overrides):
                result, _, _ = run([robots(), api([article(**overrides)])])
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["items"], [])

    def test_huge_valid_json_integer_fails_safely_and_persists_cooldown(self):
        result, fake, _ = run([robots(), api([article(preis=10**400)])])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["items"], [])
        self.assertEqual(result["warnings"][0]["code"], "invalid_payload")
        self.assertEqual(result["source_state"]["next_allowed_at"], "2026-11-07T12:09:00Z")
        self.assertEqual(len(fake.requests), 2)

    def test_explicit_empty_distinguished_from_invalid_envelope(self):
        result, _, _ = run([robots(), api([]), api([])])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["coverage"]["source_total_reported"], 0)
        self.assertEqual(result["coverage"]["pages_fetched"], 2)
        for payload in ({}, {"gesamt": 5, "artikel": []}, {"gesamt": 0, "artikel": "bad"}, {"gesamt": True, "artikel": []}):
            with self.subTest(payload=payload):
                failed, _, _ = run([robots(), api(payload=payload)])
                self.assertEqual(failed["status"], "error")
                self.assertEqual(failed["coverage"]["pages_fetched"], 0)

    def test_explicit_payload_errors_and_filter_warnings_reject_page(self):
        for metadata in ({"fehler": "private upstream error"}, {"errors": [{"message": "secret"}]},
                         {"warnings": ["unknown filter"]}, {"success": False}):
            with self.subTest(metadata=metadata):
                result, _, _ = run([robots(), api(payload={"gesamt": 1, "artikel": [article()], **metadata})])
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["items"], [])
                self.assertNotIn("private upstream error", json.dumps(result))
                self.assertNotIn("secret", json.dumps(result))

    def test_real_adapter_dto_survives_normalizer_and_pipeline(self):
        source_result, _, _ = run([robots(), api(), api([])])
        normalized = normalize_item(source_result["items"][0], NOW)
        self.assertIsNotNone(normalized)
        self.assertEqual(normalized["verification_level"], "aggregator")
        self.assertEqual(normalized["last_seen_at"], "2026-11-07T11:45:00Z")
        self.assertEqual(normalized["price_kind"], "auction_price")
        feed, state = run_pipeline(SETTINGS, source="aog", at=NOW,
            environ={"HUNTER_PUBLISH_LIVE_DATA": "true", "HUNTER_LLM_ENABLED": "false"},
            aog_collect=lambda settings, at, source_state: source_result)
        self.assertEqual(len(feed["deals"]), 1)
        self.assertEqual(feed["deals"][0]["verification_level"], "aggregator")
        self.assertEqual(state["records"][0]["last_seen_at"], "2026-11-07T11:45:00Z")


class SchedulingTests(unittest.TestCase):
    def test_six_api_requests_max_plus_robots_with_identifying_user_agent(self):
        pages = [api([article(str(336872529490 + i))], total=1128) for i in range(5)] + [api([])]
        result, fake, delays = run([robots()] + pages, settings={"aog_max_requests_per_run": 6})
        self.assertEqual(len(fake.requests), 7)
        self.assertEqual(delays, [10] * 6)
        self.assertEqual(result["coverage"]["api_requests_this_run"], 6)
        self.assertEqual(result["coverage"]["next_offset"], 240)
        self.assertEqual([parse_qs(urlsplit(req["url"]).query)["ab"][0] for req in fake.requests[1:]], ["0", "48", "96", "144", "192", "0"])
        self.assertTrue(all(req["headers"]["User-Agent"] == USER_AGENT for req in fake.requests))
        self.assertFalse(result["coverage"]["complete"])

    def test_general_and_pickup_queries_match_observed_public_ui(self):
        result, fake, _ = run([robots(), api([]), api([])])
        general, pickup = [parse_qs(urlsplit(req["url"]).query, keep_blank_values=True) for req in fake.requests[1:]]
        for params in (general, pickup):
            self.assertEqual(params["q"], [""])
            self.assertEqual(params["kat"], ["12576"])
            self.assertEqual(params["anzahl"], ["48"])
            self.assertEqual(params["sort"], ["ende"])
            self.assertEqual(params["endet"], ["alle"])
            self.assertNotIn("vmax", params)
        self.assertEqual(general["gratis"], ["0"])
        self.assertNotIn("gratis", pickup)
        self.assertEqual(pickup["abholung"], ["1"])
        self.assertEqual(pickup["plz"], ["69121"])
        self.assertEqual(pickup["km"], ["50"])
        self.assertEqual(result["coverage"]["upstream_end_filter"], "alle")
        self.assertEqual(result["coverage"]["local_active_window_hours"], 72)

    def test_short_horizon_keeps_verified_day_filter_without_inventing_72h_parameter(self):
        for lookahead, end_filter in ((1, "24h"), (24, "24h"), (24.01, "alle"), (72, "alle")):
            with self.subTest(lookahead=lookahead):
                result, fake, _ = run([robots(), api([]), api([])], settings={"lookahead_hours": lookahead})
                for request in fake.requests[1:]:
                    self.assertEqual(parse_qs(urlsplit(request["url"]).query)["endet"], [end_filter])
                self.assertEqual(result["coverage"]["local_active_window_hours"], lookahead)

    def test_next_run_refreshes_zero_and_rotates_tail(self):
        pages = [api([article()], total=1128)] * 5 + [api([])]
        result, _, _ = run([robots()] + pages, settings={"aog_max_requests_per_run": 6})
        _, fake, _ = run([robots()] + pages, settings={"aog_max_requests_per_run": 6}, state=result["source_state"], at=NOW + timedelta(minutes=31))
        offsets = [parse_qs(urlsplit(req["url"]).query)["ab"][0] for req in fake.requests[1:]]
        self.assertEqual(offsets, ["0", "240", "288", "336", "384", "0"])

    def test_debounce_before_nine_minutes_makes_zero_network_calls(self):
        first, _, _ = run([robots(), api([]), api([])])
        second, fake, _ = run([], state=first["source_state"], at=NOW + timedelta(minutes=8, seconds=59))
        self.assertEqual(second["status"], "cooldown")
        self.assertEqual(fake.requests, [])
        self.assertEqual(second["items"], [])

    def test_nine_minute_boundary_and_planned_ten_minute_cadence_are_allowed(self):
        first, _, _ = run([robots(), api([]), api([])])
        for minutes in (9, 10):
            with self.subTest(minutes=minutes):
                result, fake, _ = run([robots(), api([]), api([])], state=first["source_state"], at=NOW + timedelta(minutes=minutes))
                self.assertEqual(result["status"], "partial")
                self.assertEqual(len(fake.requests), 3)
                self.assertEqual(result["coverage"]["planned_interval_minutes"], 10)
                self.assertEqual(result["coverage"]["debounce_seconds"], 540)

    def test_legacy_thirty_minute_pause_is_not_shortened(self):
        legacy = {"last_attempt_at": "2026-11-07T12:00:00Z", "next_allowed_at": "2026-11-07T12:30:00Z"}
        for minutes in (10, 29):
            with self.subTest(minutes=minutes):
                result, fake, _ = run([], state=legacy, at=NOW + timedelta(minutes=minutes))
                self.assertEqual(result["status"], "cooldown")
                self.assertEqual(result["source_state"]["next_allowed_at"], legacy["next_allowed_at"])
                self.assertEqual(fake.requests, [])
        result, fake, _ = run([robots(), api([]), api([])], state=legacy, at=NOW + timedelta(minutes=30))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(fake.requests), 3)

    def test_default_is_four_api_requests_plus_robots_with_ten_second_gaps(self):
        pages = [api([article(str(336872529490 + i))], total=1128) for i in range(3)] + [api([])]
        fake, delays = FakeTransport([robots()] + pages), []
        result = collect({"max_price_eur": 1, "postcode": "69121", "pickup_radius_km": 50}, NOW, None,
                         transport=fake, sleep=delays.append)
        self.assertEqual(result["coverage"]["api_requests_this_run"], 4)
        self.assertEqual(result["coverage"]["requests_this_run"], 5)
        self.assertEqual(delays, [10] * 4)
        self.assertEqual([parse_qs(urlsplit(req["url"]).query)["ab"][0] for req in fake.requests[1:]], ["0", "48", "96", "0"])

    def test_144_default_runs_fit_daily_budget_at_ten_minute_intervals(self):
        state = None
        start = NOW.replace(hour=0)
        for index in range(144):
            pages = [api([article()], total=1128)] * 3 + [api([])]
            result, fake, _ = run([robots()] + pages, settings={"aog_max_requests_per_run": 4},
                                  state=state, at=start + timedelta(minutes=10 * index))
            self.assertEqual(result["status"], "partial")
            self.assertEqual(len(fake.requests), 5)
            state = result["source_state"]
        self.assertEqual(state["requests_today"], 720)
        self.assertEqual(result["coverage"]["daily_request_budget"], 750)

    def test_daily_limit_stops_before_request_751(self):
        state = {"request_day": "2026-11-07", "requests_today": 749}
        result, fake, _ = run([robots()], state=state)
        self.assertEqual(len(fake.requests), 1)
        self.assertEqual(result["source_state"]["requests_today"], 750)
        self.assertEqual(result["status"], "cooldown")
        result, fake, _ = run([], state={"request_day": "2026-11-07", "requests_today": 750})
        self.assertEqual(fake.requests, [])
        self.assertEqual(result["status"], "cooldown")

    def test_small_result_set_does_not_repeat_pages_to_fill_budget(self):
        result, fake, _ = run([robots(), api(), api([])], settings={"aog_max_requests_per_run": 6})
        self.assertEqual(len(fake.requests), 3)
        self.assertEqual(result["coverage"]["pages_fetched"], 2)

    def test_invalid_or_secret_source_state_is_not_republished(self):
        state = {"general_next_offset": -1, "headers": {"secret": "private"}, "body": "secret document", "requests_today": "bad"}
        result, _, _ = run([robots(), api([]), api([])], state=state)
        self.assertNotIn("private", json.dumps(result))
        self.assertNotIn("secret document", json.dumps(result))
        self.assertEqual(result["source_state"]["general_next_offset"], 48)

    def test_page_budget_cannot_be_raised_above_verified_bound(self):
        for settings in ({"aog_max_requests_per_run": 7}, {"aog_page_size": 1000}, {"max_price_eur": float("inf")},
                         {"max_price_eur": 10**400}, {"pickup_radius_km": 10**400}, {"postcode": "69121,evil"},
                         {"lookahead_hours": True}, {"lookahead_hours": float("inf")}, {"lookahead_hours": 10**400},
                         {"lookahead_hours": 0}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                run([], settings=settings)


class HttpSafetyTests(unittest.TestCase):
    def test_429_persists_retry_after_and_is_not_retried(self):
        result, fake, _ = run([robots(), api(status=429, headers={"Retry-After": "7200"})])
        self.assertEqual(len(fake.requests), 2)
        self.assertEqual(result["status"], "cooldown")
        self.assertGreaterEqual(result["source_state"]["next_allowed_at"], "2026-11-07T14:00:00Z")
        second, fake, _ = run([], state=result["source_state"], at=NOW + timedelta(hours=1))
        self.assertEqual(second["status"], "cooldown")
        self.assertEqual(fake.requests, [])

    def test_403_and_challenge_html_are_not_retried(self):
        for response in (api(status=403), HttpResponse(200, {"Content-Type": "text/html"}, b"<html>captcha</html>")):
            with self.subTest(response=response):
                result, fake, _ = run([robots(), response])
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["items"], [])
                self.assertEqual(len(fake.requests), 2)

    def test_access_denial_pause_still_blocks_ten_minute_run(self):
        result, _, _ = run([robots(), api(status=403)])
        later, fake, _ = run([], state=result["source_state"], at=NOW + timedelta(minutes=10))
        self.assertEqual(later["status"], "cooldown")
        self.assertEqual(fake.requests, [])
        self.assertGreaterEqual(result["source_state"]["next_allowed_at"], "2026-11-08T12:00:00Z")

    def test_missing_retry_after_keeps_thirty_minute_rate_limit_backoff(self):
        result, _, _ = run([robots(), api(status=429)])
        self.assertGreaterEqual(result["source_state"]["next_allowed_at"], "2026-11-07T12:30:00Z")
        later, fake, _ = run([], state=result["source_state"], at=NOW + timedelta(minutes=10))
        self.assertEqual(later["status"], "cooldown")
        self.assertEqual(fake.requests, [])

    def test_retry_after_longer_than_31_days_is_not_shortened(self):
        result, _, _ = run([robots(), api(status=429, headers={"Retry-After": str(46 * 86400)})])
        self.assertGreaterEqual(result["source_state"]["next_allowed_at"], "2026-12-23T12:00:00Z")
        later, fake, _ = run([], state=result["source_state"], at=NOW + timedelta(days=32))
        self.assertEqual(later["status"], "cooldown")
        self.assertEqual(fake.requests, [])

    def test_unrepresentable_retry_after_pauses_indefinitely_not_early(self):
        result, _, _ = run([robots(), api(status=429, headers={"Retry-After": "9" * 200})])
        self.assertEqual(result["source_state"]["next_allowed_at"], "9999-12-31T23:59:59Z")
        later, fake, _ = run([], state=result["source_state"], at=NOW + timedelta(days=100))
        self.assertEqual(later["status"], "cooldown")
        self.assertEqual(fake.requests, [])

    def test_redirects_never_followed(self):
        result, fake, _ = run([robots(), api(status=302, headers={"Location": "https://www.ebay.de/"})])
        self.assertEqual(result["status"], "error")
        self.assertEqual(len(fake.requests), 2)
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "", {}, "https://www.ebay.de/"))

    def test_robot_and_json_body_bounds(self):
        for responses in ([HttpResponse(200, {}, b"x" * (MAX_ROBOTS_BYTES + 1))],
                          [robots(), HttpResponse(200, {"Content-Type": "application/json"}, b"x" * (MAX_JSON_BYTES + 1))]):
            result, fake, _ = run(responses)
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["warnings"][0]["code"], "response_too_large")

    def test_transport_error_never_leaks_its_message(self):
        result, fake, _ = run([robots(), OSError("private auth token")])
        self.assertEqual(result["status"], "error")
        self.assertNotIn("private auth token", json.dumps(result))
        self.assertEqual(len(fake.requests), 2)

    def test_later_page_error_preserves_only_valid_previous_page(self):
        result, fake, _ = run([robots(), api(), api(payload={"malformed": "payload"})])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["coverage"]["pages_fetched"], 1)
        self.assertEqual(len(fake.requests), 3)


if __name__ == "__main__":
    unittest.main()
