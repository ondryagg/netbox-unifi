"""Integration API transport: key probing, pagination and error handling (Network API v10.6.106)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from netbox_unifi.services.unifi.device import Device
from netbox_unifi.services.unifi.unifi import Unifi
from tests.integration_api_fixtures import SITE

BASE = "https://controller.example.com"
INTEGRATION_BASE = f"{BASE}/proxy/network/integration/v1"
APP_INFO = {"applicationVersion": "10.6.106"}


class _Resp:
    def __init__(self, status_code=200, payload=None, reason="OK", headers=None, path="/"):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = "{}" if payload is None else "payload"
        self.reason = reason
        self.headers = headers or {}
        self.request = type("Req", (), {"path_url": path})()

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("UNIFI_VERIFY_SSL", "UNIFI_PERSIST_SESSION"):
        monkeypatch.delenv(key, raising=False)


def _build_unifi(**kwargs):
    with patch.object(Unifi, "load_session_from_file", return_value=None), patch.object(
        Unifi, "configure_integration_api", return_value=True
    ), patch.object(Unifi, "get_sites", return_value={}):
        return Unifi(BASE, api_key="api-key", **kwargs)


def _configured_unifi():
    unifi = _build_unifi()
    unifi.integration_api_base = INTEGRATION_BASE
    unifi.integration_auth_headers = {"X-API-KEY": "api-key"}
    return unifi


def _probe(unifi, route):
    """Run the real configure_integration_api() with session.get answered by `route(url)`."""
    unifi.integration_api_base = None
    unifi.integration_auth_headers = {}
    calls = []

    def fake_get(url, headers=None, **_kwargs):
        calls.append((url, dict(headers or {})))
        return route(url)

    with patch.object(unifi.session, "get", side_effect=fake_get):
        return unifi.configure_integration_api(), calls


def test_probe_uses_x_api_key_on_the_proxy_path_first():
    unifi = _build_unifi()

    ok, calls = _probe(unifi, lambda url: _Resp(200, APP_INFO))

    assert ok is True
    assert len(calls) == 1
    url, headers = calls[0]
    assert url == f"{INTEGRATION_BASE}/info"
    assert headers["X-API-KEY"] == "api-key"
    assert unifi.integration_api_base == INTEGRATION_BASE
    assert unifi.integration_auth_headers == {"X-API-KEY": "api-key"}


def test_probe_falls_back_to_the_unifi_os_less_path():
    unifi = _build_unifi()

    def route(url):
        if "/proxy/network/" in url:
            return _Resp(404, {"message": "not found"})
        return _Resp(200, APP_INFO)

    ok, _calls = _probe(unifi, route)

    assert ok is True
    assert unifi.integration_api_base == f"{BASE}/integration/v1"


def test_probe_accepts_the_sites_endpoint_when_info_is_missing():
    unifi = _build_unifi()

    def route(url):
        if url.endswith("/info"):
            return _Resp(404, {"message": "not found"})
        return _Resp(200, {"data": [], "totalCount": 0, "offset": 0, "limit": 1, "count": 0})

    ok, _calls = _probe(unifi, route)

    assert ok is True
    assert unifi.integration_api_base == INTEGRATION_BASE


def test_probe_rejects_a_key_the_console_does_not_accept():
    unifi = _build_unifi()

    ok, calls = _probe(unifi, lambda url: _Resp(401, {"message": "Unauthorized"}))

    assert ok is False
    assert unifi.integration_api_base is None
    assert calls  # every base/header combination was tried


def test_probe_sends_a_custom_header_first():
    unifi = _build_unifi(api_key_header="X-Custom-Key")

    ok, calls = _probe(unifi, lambda url: _Resp(200, APP_INFO))

    assert ok is True
    assert calls[0][1]["X-Custom-Key"] == "api-key"
    assert unifi.integration_auth_headers == {"X-Custom-Key": "api-key"}


def test_sites_are_paginated_and_found_by_name_reference_or_id():
    unifi = _build_unifi()
    pages = {
        0: {
            "offset": 0, "limit": 2, "count": 2, "totalCount": 3,
            "data": [
                {"id": "id-1", "internalReference": "default", "name": "Default"},
                {"id": "id-2", "internalReference": "abc123", "name": "Branch"},
            ],
        },
        2: {
            "offset": 2, "limit": 2, "count": 1, "totalCount": 3,
            "data": [{"id": "id-3", "internalReference": "xyz789", "name": "Warehouse"}],
        },
    }
    requested = []

    def fake_request(endpoint, method="GET", params=None, **_kwargs):
        requested.append((endpoint, dict(params)))
        return pages[params["offset"]]

    with patch.object(unifi, "make_request", side_effect=fake_request):
        unifi.sites = unifi._get_sites_integration()

    assert requested == [("/sites", {"offset": 0, "limit": 200}), ("/sites", {"offset": 2, "limit": 200})]
    assert list(unifi.sites) == ["Default", "Branch", "Warehouse"]
    assert unifi.sites["Branch"].api_id == "id-2"
    assert unifi.site("abc123") is unifi.sites["Branch"]
    assert unifi["id-3"] is unifi.sites["Warehouse"]


def test_resource_listing_pages_through_results_and_forwards_the_filter():
    pages = {
        0: {"offset": 0, "limit": 2, "count": 2, "totalCount": 3, "data": [{"id": "d1"}, {"id": "d2"}]},
        2: {"offset": 2, "limit": 2, "count": 1, "totalCount": 3, "data": [{"id": "d3"}]},
    }
    requested = []

    class RecordingUnifi:
        api_style = "integration"

        def make_request(self, endpoint, method="GET", params=None, **_kwargs):
            requested.append((endpoint, dict(params)))
            return pages[params["offset"]]

    devices = Device(RecordingUnifi(), SITE).all(filter_query="state.eq('ONLINE')", limit=2)

    assert [device["id"] for device in devices] == ["d1", "d2", "d3"]
    assert requested == [
        ("/sites/site-1/devices", {"offset": 0, "limit": 2, "filter": "state.eq('ONLINE')"}),
        ("/sites/site-1/devices", {"offset": 2, "limit": 2, "filter": "state.eq('ONLINE')"}),
    ]


def test_error_message_body_is_returned_with_its_documented_fields():
    unifi = _configured_unifi()
    error_body = {
        "statusCode": 404,
        "statusName": "NOT_FOUND",
        "code": "api.err.not-found",
        "message": "Network not found",
        "timestamp": "2026-10-25T12:00:00.000Z",
        "requestPath": "/integration/v1/sites/s/networks/n",
        "requestId": "0b7c1e4a-9d57-4c0e-8f56-2f0c3a4d9e11",
    }
    response = _Resp(404, error_body, reason="Not Found", path="/proxy/network/integration/v1/sites/s/networks/n")

    with patch.object(unifi.session, "request", return_value=response) as request_mock:
        result = unifi._make_request_integration("/sites/s/networks/n")

    assert request_mock.call_count == 1  # 404 is not retried
    assert result == error_body


def test_unauthorized_response_reprobes_once_then_returns_the_error():
    unifi = _configured_unifi()
    unauthorized = {"statusCode": 401, "statusName": "UNAUTHORIZED", "code": "api.err.unauthorized", "message": "bad key"}

    with patch.object(
        unifi.session, "request", side_effect=[_Resp(401, unauthorized), _Resp(401, unauthorized)]
    ) as request_mock, patch.object(unifi, "configure_integration_api", return_value=True) as reprobe:
        result = unifi._make_request_integration("/sites")

    assert reprobe.call_count == 1
    assert request_mock.call_count == 2
    assert result["statusCode"] == 401
    assert result["code"] == "api.err.unauthorized"


def test_unauthorized_response_recovers_when_the_reprobe_finds_a_working_header():
    unifi = _configured_unifi()
    page = {"offset": 0, "limit": 200, "count": 0, "totalCount": 0, "data": []}

    with patch.object(
        unifi.session, "request", side_effect=[_Resp(401, {"message": "bad key"}), _Resp(200, page)]
    ), patch.object(unifi, "configure_integration_api", return_value=True):
        assert unifi._make_request_integration("/sites") == page
