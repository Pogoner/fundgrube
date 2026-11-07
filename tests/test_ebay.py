"""Hermetic adapter tests: every HTTP call uses an in-memory fake transport."""

import base64
import json
import os
import traceback
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from hunter.ebay import (
    BASE_API, MAX_RESPONSE_BYTES, ApiError, BudgetExceeded, CredentialsMissing,
    EbayClient, HttpResponse, RateLimitError, _NoRedirect, _production_url, _retry_after,
)


NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)
ITEM_ID = "v1|123456789012|0"


def response(payload=None, status=200, headers=None):
    return HttpResponse(status, headers or {}, json.dumps(payload if payload is not None else {}).encode())


def token(value="fake-bearer-token"):
    return response({"access_token": value, "expires_in": 7200, "token_type": "Application Access Token"})


def collection(items=None, total=None, **extra):
    items = items if items is not None else [{"itemId": ITEM_ID, "title": "Test item"}]
    return response({"itemSummaries": items, "total": len(items) if total is None else total, **extra})


class QueueTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, **request):
        self.calls.append(request)
        if not self.responses:
            raise AssertionError("Unexpected mock HTTP request")
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def client_with(*responses, **kwargs):
    transport = QueueTransport(*responses)
    delays = []
    client = EbayClient(
        "fake-client-id", "fake-client-secret", transport=transport,
        sleep=delays.append, **kwargs,
    )
    return client, transport, delays


def search(client, **overrides):
    args = {
        "category_id": "181998", "now": NOW, "lookahead_hours": 24,
        "max_price_eur": None, "max_bids": 0, "local_pickup": False,
        "postcode": "69121", "pickup_radius_km": 50,
    }
    args.update(overrides)
    return client.search(**args)


class TestAuthentication(unittest.TestCase):
    def test_client_credentials_request_and_token_reuse(self):
        client, transport, _ = client_with(token(), collection(), collection())
        search(client)
        search(client)
        oauth = transport.calls[0]
        self.assertEqual(oauth["method"], "POST")
        self.assertEqual(oauth["url"], BASE_API + "/identity/v1/oauth2/token")
        self.assertEqual(oauth["headers"]["Content-Type"], "application/x-www-form-urlencoded")
        expected = base64.b64encode(b"fake-client-id:fake-client-secret").decode()
        self.assertEqual(oauth["headers"]["Authorization"], "Basic " + expected)
        self.assertEqual(parse_qs(oauth["body"].decode()), {
            "grant_type": ["client_credentials"],
            "scope": ["https://api.ebay.com/oauth/api_scope"],
        })
        self.assertEqual(client.calls_used, 2)
        self.assertEqual(client.oauth_calls_used, 1)
        self.assertEqual(client.taxonomy_calls_used, 0)
        self.assertEqual(transport.calls[1]["headers"]["Authorization"], "Bearer fake-bearer-token")

    def test_environment_credentials(self):
        transport = QueueTransport(token(), collection())
        with patch.dict(os.environ, {"EBAY_CLIENT_ID": "env-id", "EBAY_CLIENT_SECRET": "env-secret"}):
            client = EbayClient(transport=transport)
        search(client)
        expected = base64.b64encode(b"env-id:env-secret").decode()
        self.assertEqual(transport.calls[0]["headers"]["Authorization"], "Basic " + expected)

    def test_missing_credentials_never_calls_transport(self):
        transport = QueueTransport()
        with patch.dict(os.environ, {}, clear=True):
            client = EbayClient(transport=transport)
        with self.assertRaises(CredentialsMissing):
            search(client)
        self.assertEqual(client.calls_used, 0)
        self.assertEqual(transport.calls, [])

    def test_explicit_empty_credentials_do_not_fall_back_to_environment(self):
        with patch.dict(os.environ, {"EBAY_CLIENT_ID": "configured", "EBAY_CLIENT_SECRET": "configured"}):
            client = EbayClient("", "", transport=QueueTransport())
        with self.assertRaises(CredentialsMissing):
            search(client)

    def test_token_expiry_triggers_refresh(self):
        client, _, _ = client_with(token("first"), collection(), token("second"), collection())
        with patch("hunter.ebay.time.monotonic", return_value=100):
            search(client)
        with patch("hunter.ebay.time.monotonic", return_value=7400):
            search(client)
        self.assertEqual(client.oauth_calls_used, 2)
        self.assertEqual(client.calls_used, 2)

    def test_401_refreshes_once_and_counts_both_browse_attempts(self):
        client, transport, _ = client_with(token("old"), response(status=401), token("new"), collection())
        search(client)
        self.assertEqual(client.calls_used, 2)
        self.assertEqual(client.oauth_calls_used, 2)
        self.assertEqual(transport.calls[-1]["headers"]["Authorization"], "Bearer new")

    def test_repeated_401_is_not_an_unbounded_refresh_loop(self):
        client, transport, _ = client_with(token(), response(status=401), token(), response(status=401))
        with self.assertRaises(ApiError) as result:
            search(client)
        self.assertEqual(result.exception.status_code, 401)
        self.assertEqual(len(transport.calls), 4)

    def test_bad_token_response_and_header_injection_rejected(self):
        cases = [
            {}, {"access_token": "a"}, {"access_token": "a", "expires_in": 0},
            {"access_token": "a", "expires_in": "7200"},
            {"access_token": "a\r\nInjected: bad", "expires_in": 7200},
            {"access_token": "a", "expires_in": float("inf")},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                client, transport, _ = client_with(response(payload))
                with self.assertRaises(ApiError):
                    search(client)
                self.assertEqual(client.calls_used, 0)
                self.assertEqual(len(transport.calls), 1)


class TestSearch(unittest.TestCase):
    def test_delivery_query_and_context_header(self):
        client, transport, _ = client_with(token(), collection())
        result = search(client)
        request = transport.calls[-1]
        query = parse_qs(urlsplit(request["url"]).query)
        filters = query["filter"][0]
        self.assertEqual(query["category_ids"], ["181998"])
        self.assertEqual(query["sort"], ["endingSoonest"])
        self.assertEqual(query["fieldgroups"], ["EXTENDED"])
        self.assertEqual(query["limit"], ["200"])
        self.assertIn("buyingOptions:{AUCTION}", filters)
        self.assertIn("bidCount:[..0]", filters)
        self.assertIn("itemEndDate:[2026-11-07T12:00:00.000Z..2026-11-08T12:00:00.000Z]", filters)
        self.assertIn("deliveryCountry:DE,deliveryPostalCode:69121", filters)
        self.assertNotIn("price:", filters)
        self.assertNotIn("priceCurrency", filters)
        self.assertNotIn("pickup", filters)
        self.assertEqual(request["headers"]["X-EBAY-C-MARKETPLACE-ID"], "EBAY_DE")
        self.assertEqual(request["headers"]["X-EBAY-C-ENDUSERCTX"], "contextualLocation=country%3DDE%2Czip%3D69121")
        self.assertFalse(result["price_filter_applied"])

    def test_all_five_pickup_filters_in_km(self):
        client, transport, _ = client_with(token(), collection())
        search(client, local_pickup=True, postcode="69115", pickup_radius_km=25)
        filters = parse_qs(urlsplit(transport.calls[-1]["url"]).query)["filter"][0]
        for expected in (
            "deliveryOptions:{SELLER_ARRANGED_LOCAL_PICKUP}", "pickupCountry:DE",
            "pickupPostalCode:69115", "pickupRadius:25", "pickupRadiusUnit:km",
        ):
            self.assertIn(expected, filters)
        self.assertNotIn("deliveryCountry", filters)
        self.assertNotIn("deliveryPostalCode", filters)
        self.assertIn("zip%3D69115", transport.calls[-1]["headers"]["X-EBAY-C-ENDUSERCTX"])

    def test_optional_price_cap_is_explicit_and_flagged(self):
        client, transport, _ = client_with(token(), collection())
        result = search(client, max_price_eur="1.00")
        filters = parse_qs(urlsplit(transport.calls[-1]["url"]).query)["filter"][0]
        self.assertIn("price:[..1.00],priceCurrency:EUR", filters)
        self.assertTrue(result["price_filter_applied"])

    def test_timezone_is_converted_to_utc(self):
        client, transport, _ = client_with(token(), collection())
        search(client, now=NOW.astimezone(timezone(timedelta(hours=1))))
        self.assertIn("2026-11-07T12%3A00%3A00.000Z", transport.calls[-1]["url"])

    def test_raw_auction_fields_and_missing_values_are_preserved(self):
        raw = {"itemId": ITEM_ID, "buyingOptions": ["AUCTION", "FIXED_PRICE"],
               "price": {"value": "250", "currency": "EUR"},
               "currentBidPrice": {"value": "1", "currency": "EUR"}}
        client, _, _ = client_with(token(), collection([raw]))
        result = search(client)
        self.assertEqual(result["items"], [raw])
        self.assertNotIn("bidCount", result["items"][0])
        self.assertNotIn("reservePriceMet", result["items"][0])
        self.assertEqual(result["items"][0]["price"]["value"], "250")

    def test_pagination_uses_offsets_and_does_not_follow_provider_next_url(self):
        client, transport, _ = client_with(token(), collection(total=900, next="https://evil.invalid/steal"))
        result = search(client, offset=200)
        self.assertTrue(result["has_more"])
        self.assertEqual(result["next_offset"], 400)
        self.assertEqual(result["total"], 900)
        self.assertEqual(len(transport.calls), 2)
        self.assertTrue(all(call["url"].startswith(BASE_API + "/") for call in transport.calls))

    def test_search_stops_at_10000_cap(self):
        client, _, _ = client_with(token(), collection(total=20_000))
        result = search(client, offset=9800)
        self.assertFalse(result["has_more"])
        self.assertIsNone(result["next_offset"])
        self.assertTrue(result["capped"])

    def test_empty_results(self):
        client, _, _ = client_with(token(), collection([]))
        result = search(client)
        self.assertEqual(result["items"], [])
        self.assertEqual(result["total"], 0)
        self.assertFalse(result["has_more"])

    def test_warnings_fail_closed_for_ignored_filters(self):
        client, _, _ = client_with(token(), collection(warnings=[{"errorId": 12010, "message": "secret text"}]))
        with self.assertRaises(ApiError) as result:
            search(client, local_pickup=True)
        self.assertNotIn("secret text", str(result.exception))

    def test_invalid_collections_are_not_treated_as_zero_results(self):
        for payload in ({}, {"total": "0"}, {"total": False}, {"total": -1},
                        {"total": 1, "itemSummaries": ["invalid"]},
                        {"total": 1, "itemSummaries": {"itemId": ITEM_ID}}):
            with self.subTest(payload=payload):
                client, _, _ = client_with(token(), response(payload))
                with self.assertRaises(ApiError):
                    search(client)

    def test_invalid_search_input_cannot_inject_filters_or_paths(self):
        cases = (
            {"category_id": "1,2"}, {"category_id": "../bad"}, {"category_id": True},
            {"postcode": "69121,pickupCountry:US"}, {"postcode": 69121},
            {"max_bids": -1}, {"max_bids": False}, {"max_price_eur": float("nan")},
            {"max_price_eur": "1],sellers:{bad}"}, {"max_price_eur": -1},
            {"local_pickup": "false"}, {"pickup_radius_km": 0},
            {"lookahead_hours": 0}, {"now": NOW.replace(tzinfo=None)},
            {"limit": 201}, {"offset": 1}, {"offset": 10_000},
        )
        for values in cases:
            with self.subTest(values=values):
                client, transport, _ = client_with()
                with self.assertRaises(ValueError):
                    search(client, **values)
                self.assertEqual(transport.calls, [])


class TestTaxonomy(unittest.TestCase):
    def test_subtree_only_confirmed_leaves_sorted_and_cached(self):
        tree = {"categorySubtreeNode": {
            "category": {"categoryId": "12576", "categoryName": "Gewerbe & Industrie"},
            "childCategoryTreeNodes": [
                {"category": {"categoryId": "20", "categoryName": "Werkzeuge"}, "leafCategoryTreeNode": True},
                {"category": {"categoryId": "4", "categoryName": "Messgeräte"}, "childCategoryTreeNodes": [
                    {"category": {"categoryId": "9", "categoryName": "Multimeter"}, "leafCategoryTreeNode": True},
                ]},
            ],
        }}
        client, transport, _ = client_with(token(), response({"categoryTreeId": "77"}), response(tree))
        result = client.leaf_categories("12576")
        self.assertEqual(result, [{"id": "9", "name": "Multimeter"}, {"id": "20", "name": "Werkzeuge"}])
        result[0]["name"] = "modified by caller"
        self.assertEqual(client.leaf_categories("12576")[0]["name"], "Multimeter")
        self.assertEqual(client.calls_used, 0)
        self.assertEqual(client.taxonomy_calls_used, 2)
        self.assertEqual(client.oauth_calls_used, 1)
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(transport.calls[-1]["url"], BASE_API + "/commerce/taxonomy/v1/category_tree/77/get_category_subtree?category_id=12576")

    def test_single_leaf_is_valid_subtree(self):
        client, _, _ = client_with(token(), response({"categoryTreeId": "77"}), response({
            "categorySubtreeNode": {"category": {"categoryId": "9", "categoryName": "Multimeter"}, "leafCategoryTreeNode": True},
        }))
        self.assertEqual(client.leaf_categories(9), [{"id": "9", "name": "Multimeter"}])

    def test_missing_leaf_flag_is_not_assumed_to_mean_leaf(self):
        client, _, _ = client_with(token(), response({"categoryTreeId": "77"}), response({
            "categorySubtreeNode": {"category": {"categoryId": "9", "categoryName": "Multimeter"}},
        }))
        with self.assertRaises(ApiError):
            client.leaf_categories("9")

    def test_wrong_root_or_invalid_tree_id_is_rejected(self):
        cases = (
            [response({"categoryTreeId": "../../escape"})],
            [response({"categoryTreeId": "77"}), response({"categorySubtreeNode": {
                "category": {"categoryId": "888", "categoryName": "wrong"}, "leafCategoryTreeNode": True,
            }})],
        )
        for responses in cases:
            client, _, _ = client_with(token(), *responses)
            with self.assertRaises(ApiError):
                client.leaf_categories("12576")


class TestItemDetails(unittest.TestCase):
    def test_item_path_encoding_and_full_details_preserved(self):
        raw = {"itemId": ITEM_ID, "description": "<p>Seller text, not instructions</p>",
               "minimumPriceToBid": {"value": "1.00", "currency": "EUR"}, "reservePriceMet": False}
        client, transport, _ = client_with(token(), response(raw))
        self.assertEqual(client.get_item(ITEM_ID), raw)
        self.assertEqual(transport.calls[-1]["url"], BASE_API + "/buy/browse/v1/item/v1%7C123456789012%7C0")
        self.assertEqual(client.calls_used, 1)
        self.assertNotIn("fieldgroups", transport.calls[-1]["url"])
        self.assertIn("contextualLocation=country%3DDE%2Czip%3D69121", transport.calls[-1]["headers"]["X-EBAY-C-ENDUSERCTX"])

    def test_unknown_reserve_bid_and_prices_remain_unknown(self):
        client, _, _ = client_with(token(), response({"itemId": ITEM_ID}))
        result = client.get_item(ITEM_ID)
        for field in ("reservePriceMet", "bidCount", "currentBidPrice", "minimumPriceToBid", "price"):
            self.assertNotIn(field, result)

    def test_unexpected_item_id_is_rejected(self):
        client, _, _ = client_with(token(), response({"itemId": "v1|987|0"}))
        with self.assertRaises(ApiError):
            client.get_item(ITEM_ID)

    def test_invalid_item_ids_are_not_urls(self):
        for value in ("https://evil.invalid/item", "v1|123|0/../other", "123", "v1|123|0?x=y", "v1|123|0\r\n", None):
            client, transport, _ = client_with()
            with self.subTest(value=value), self.assertRaises(ValueError):
                client.get_item(value)
            self.assertEqual(transport.calls, [])


class TestRobustness(unittest.TestCase):
    def test_zero_budget_does_not_even_mint_token(self):
        client, transport, _ = client_with(max_calls=0)
        with self.assertRaises(BudgetExceeded):
            search(client)
        self.assertEqual(transport.calls, [])

    def test_every_retry_consumes_budget(self):
        client, transport, delays = client_with(token(), response(status=503), max_calls=1)
        with self.assertRaises(BudgetExceeded):
            search(client)
        self.assertEqual(client.calls_used, 1)
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(delays, [1])

    def test_budget_exhaustion_prevents_token_refresh(self):
        client, transport, _ = client_with(token(), response(status=401), max_calls=1)
        with self.assertRaises(BudgetExceeded):
            search(client)
        self.assertEqual(client.oauth_calls_used, 1)
        self.assertEqual(len(transport.calls), 2)

    def test_transient_http_error_retries_are_bounded(self):
        client, _, delays = client_with(token(), response(status=503), response(status=502), collection())
        search(client)
        self.assertEqual(delays, [1, 2])
        self.assertEqual(client.calls_used, 3)
        exhausted, _, waits = client_with(token(), response(status=500), response(status=500), response(status=500))
        with self.assertRaises(ApiError) as result:
            search(exhausted)
        self.assertEqual(result.exception.status_code, 500)
        self.assertEqual(waits, [1, 2])
        self.assertEqual(exhausted.calls_used, 3)

    def test_short_rate_limit_retry_after_is_respected(self):
        client, _, delays = client_with(token(), response(status=429, headers={"Retry-After": "2"}), collection())
        search(client)
        self.assertEqual(delays, [2])
        self.assertEqual(client.calls_used, 2)

    def test_long_rate_limit_is_surfaced_without_sleep(self):
        client, _, delays = client_with(token(), response(status=429, headers={"retry-after": "3600"}))
        with self.assertRaises(RateLimitError) as result:
            search(client)
        self.assertEqual(result.exception.retry_after, 3600)
        self.assertEqual(delays, [])
        self.assertEqual(client.calls_used, 1)

    def test_rate_limit_retry_count_is_bounded(self):
        client, _, delays = client_with(token(), response(status=429), response(status=429), response(status=429))
        with self.assertRaises(RateLimitError):
            search(client)
        self.assertEqual(delays, [1, 2])
        self.assertEqual(client.calls_used, 3)

    def test_retry_after_supports_http_date_and_invalid_values(self):
        self.assertEqual(_retry_after({"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}), 0)
        self.assertIsNone(_retry_after({"Retry-After": "secret-token"}))
        self.assertIsNone(_retry_after({}))

    def test_network_errors_are_redacted_in_public_traceback(self):
        secret = "fake-client-secret"
        client, _, _ = client_with(token(), OSError(secret + " Bearer private-token"), max_retries=0)
        with self.assertRaises(ApiError) as result:
            search(client)
        rendered = "".join(traceback.format_exception(result.exception))
        self.assertNotIn(secret, rendered)
        self.assertNotIn("private-token", rendered)

    def test_http_error_payload_is_not_echoed(self):
        client, _, _ = client_with(token(), response({
            "errors": [{"errorId": 1100, "message": "fake-client-secret", "parameters": [{"value": "private-token"}]}],
        }, status=403))
        with self.assertRaises(ApiError) as result:
            search(client)
        self.assertEqual(result.exception.error_ids, (1100,))
        self.assertEqual(result.exception.status_code, 403)
        self.assertNotIn("fake-client-secret", str(result.exception))
        self.assertNotIn("private-token", str(result.exception))

    def test_body_errors_even_with_http_200_are_not_success(self):
        client, _, _ = client_with(token(), response({"errors": [{"errorId": 42}]}))
        with self.assertRaises(ApiError):
            search(client)

    def test_non_json_and_oversized_responses_rejected(self):
        for payload in (HttpResponse(200, {}, b"not JSON"), response([]),
                        HttpResponse(200, {}, b"x" * (MAX_RESPONSE_BYTES + 1))):
            client, _, _ = client_with(token(), payload)
            with self.assertRaises(ApiError):
                search(client)

    def test_redirect_status_is_never_followed(self):
        client, transport, _ = client_with(token(), response(status=302, headers={"Location": "https://evil.invalid/"}))
        with self.assertRaises(ApiError) as result:
            search(client)
        self.assertEqual(result.exception.status_code, 302)
        self.assertEqual(len(transport.calls), 2)
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.invalid/"))

    def test_fixed_production_origin(self):
        for path in ("https://evil.invalid", "//evil.invalid", "/path?token=bad", "/../secret", "/path\r\nBad: yes"):
            with self.subTest(path=path), self.assertRaises(ApiError):
                _production_url(path)

    def test_response_repr_has_no_sensitive_body_or_headers(self):
        result = HttpResponse(401, {"Authorization": "secret"}, b"secret")
        self.assertNotIn("secret", repr(result))

    def test_invalid_client_config(self):
        for kwargs in ({"max_calls": -1}, {"max_calls": True}, {"max_retries": 99},
                       {"timeout": 61}, {"marketplace": "EBAY_US"}, {"postcode": "69121\n"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                client_with(**kwargs)


if __name__ == "__main__":
    unittest.main()
