"""Shopify layer: webhook HMAC, session tokens, GraphQL client, response parsing."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

import httpx
import jwt
import pytest

from app.core.errors import AuthenticationError
from app.shopify.client import ShopifyGraphQLClient, raise_on_user_errors
from app.shopify.errors import (
    ShopifyAuthError,
    ShopifyGraphQLError,
    ShopifyTransientError,
    ShopifyUserError,
)
from app.shopify.parse import parse_order, weight_to_grams
from app.shopify.security import compute_webhook_hmac, verify_session_token, verify_webhook_hmac
from tests.factories import DUPLICATE_HOLD, fo_json, order_json

SECRET = "test-client-secret-0123456789abcdef"
CLIENT_ID = "test-client-id"
SHOP = "trekiva-test.myshopify.com"


# --- webhook HMAC ---------------------------------------------------------------------------


def test_webhook_hmac_roundtrip() -> None:
    body = b'{"id": 1}'
    assert verify_webhook_hmac(body, compute_webhook_hmac(body, SECRET), SECRET)


@pytest.mark.parametrize(
    "header", [None, "", "not-base64", compute_webhook_hmac(b'{"id": 2}', SECRET)]
)
def test_webhook_hmac_rejects_bad_signatures(header: str | None) -> None:
    assert not verify_webhook_hmac(b'{"id": 1}', header, SECRET)


def test_webhook_hmac_rejects_wrong_secret_and_empty_secret() -> None:
    body = b"{}"
    assert not verify_webhook_hmac(body, compute_webhook_hmac(body, "other"), SECRET)
    assert not verify_webhook_hmac(body, compute_webhook_hmac(body, ""), "")


# --- session tokens -------------------------------------------------------------------------


def make_token(**overrides: Any) -> str:
    now = int(time.time())
    claims = {
        "iss": f"https://{SHOP}/admin",
        "dest": f"https://{SHOP}",
        "aud": CLIENT_ID,
        "sub": "42",
        "exp": now + 60,
        "nbf": now - 5,
        "iat": now - 5,
        "jti": "abc",
        "sid": "session-1",
    }
    secret = overrides.pop("_secret", SECRET)
    claims.update(overrides)
    return jwt.encode(claims, secret, algorithm="HS256")


def test_valid_session_token() -> None:
    claims = verify_session_token(make_token(), client_id=CLIENT_ID, client_secret=SECRET)
    assert (claims.shop_domain, claims.user_id) == (SHOP, "42")


@pytest.mark.parametrize(
    "overrides",
    [
        {"_secret": "wrong-secret-0123456789abcdef-xyz"},
        {"aud": "another-app"},
        {"exp": int(time.time()) - 120},
        {"nbf": int(time.time()) + 120},
        {"dest": "https://evil.myshopify.com"},
        {"dest": "https://example.com", "iss": "https://example.com/admin"},
    ],
)
def test_invalid_session_tokens(overrides: dict[str, Any]) -> None:
    with pytest.raises(AuthenticationError):
        verify_session_token(make_token(**overrides), client_id=CLIENT_ID, client_secret=SECRET)


def test_session_token_with_none_algorithm_rejected() -> None:
    token = jwt.encode({"dest": f"https://{SHOP}"}, key=None, algorithm="none")  # type: ignore[arg-type]
    with pytest.raises(AuthenticationError):
        verify_session_token(token, client_id=CLIENT_ID, client_secret=SECRET)


# --- GraphQL client -------------------------------------------------------------------------


def client_with(
    responses: list[httpx.Response],
) -> tuple[ShopifyGraphQLClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responses[min(len(seen) - 1, len(responses) - 1)]

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = ShopifyGraphQLClient(
        shop_domain=SHOP, access_token="tok", api_version="2026-07", http=http, sleep=lambda s: None
    )
    return client, seen


def test_graphql_success_and_headers() -> None:
    client, seen = client_with([httpx.Response(200, json={"data": {"shop": {"name": "x"}}})])
    assert client.execute("{ shop { name } }") == {"shop": {"name": "x"}}
    assert seen[0].url.path == "/admin/api/2026-07/graphql.json"
    assert seen[0].headers["X-Shopify-Access-Token"] == "tok"


def test_graphql_throttled_then_succeeds() -> None:
    throttled = httpx.Response(
        200,
        json={
            "errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}],
            "extensions": {
                "cost": {
                    "requestedQueryCost": 50,
                    "throttleStatus": {"currentlyAvailable": 10, "restoreRate": 100},
                }
            },
        },
    )
    client, seen = client_with([throttled, httpx.Response(200, json={"data": {"ok": True}})])
    assert client.execute("{ ok }") == {"ok": True}
    assert len(seen) == 2


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(401), ShopifyAuthError),
        (httpx.Response(500), ShopifyTransientError),
        (
            httpx.Response(200, json={"errors": [{"message": "Field 'x' doesn't exist"}]}),
            ShopifyGraphQLError,
        ),
        (
            httpx.Response(
                200,
                json={"errors": [{"message": "denied", "extensions": {"code": "ACCESS_DENIED"}}]},
            ),
            ShopifyAuthError,
        ),
    ],
)
def test_graphql_errors(response: httpx.Response, error: type[Exception]) -> None:
    client, _ = client_with([response])
    with pytest.raises(error):
        client.execute("{ x }")


def test_graphql_503_retried_then_raises() -> None:
    client, seen = client_with([httpx.Response(503)])
    with pytest.raises(ShopifyTransientError):
        client.execute("{ x }")
    assert len(seen) == 5  # 1 + 4 retries


def test_user_errors_raise() -> None:
    with pytest.raises(ShopifyUserError, match="bad line item"):
        raise_on_user_errors(
            {"userErrors": [{"field": ["x"], "message": "bad line item"}]}, "fulfillmentCreate"
        )
    assert raise_on_user_errors({"userErrors": [], "ok": 1}, "op") == {"userErrors": [], "ok": 1}


# --- parsing --------------------------------------------------------------------------------


def test_parse_order_snapshot() -> None:
    raw = order_json(
        tags=["DUPLICATE-REVIEW", "vip"],
        gateways=["Cash on Delivery (COD)"],
        outstanding="1049.00",
        risk_levels=["HIGH"],
        fulfillment_orders=[
            fo_json(status="ON_HOLD", holds=[DUPLICATE_HOLD], quantity=2, weight_grams=250)
        ],
    )
    order = parse_order(raw)
    assert order.name == "TR-001500"
    assert order.has_tag("duplicate-review")
    assert order.outstanding == Decimal("1049.00")
    assert order.risk.is_high and order.risk.analysed
    fo = order.fulfillment_orders[0]
    assert fo.is_on_hold
    assert fo.holds[0].reason_notes == "Possible duplicate order (Flow)"
    assert fo.total_weight_g == 500
    assert fo.location_id == "gid://shopify/Location/1001"
    assert order.shipping_address is not None and order.shipping_address.zip == "560001"


def test_unknown_weight_gives_none() -> None:
    order = parse_order(order_json(fulfillment_orders=[fo_json(weight_grams=None)]))
    assert order.fulfillment_orders[0].total_weight_g is None


@pytest.mark.parametrize(
    ("weight", "grams"),
    [
        ({"unit": "KILOGRAMS", "value": 1.25}, 1250),
        ({"unit": "GRAMS", "value": 300}, 300),
        ({"unit": "POUNDS", "value": 1}, 454),
        ({"unit": "OUNCES", "value": 1}, 28),
        (None, None),
        ({"unit": "STONES", "value": 1}, None),
    ],
)
def test_weight_conversion(weight: dict[str, Any] | None, grams: int | None) -> None:
    assert weight_to_grams(weight) == grams
