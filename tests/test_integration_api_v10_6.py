"""Integration API v10.6.x payload shapes (https://developer.ui.com/network/v10.6.106/)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import main
from netbox_unifi.services.unifi.networkconf import NetworkConf, flatten_integration_network
from netbox_unifi.services.unifi.unifi import Unifi
from tests.integration_api_fixtures import (
    NETWORK_OVERVIEWS,
    SITE,
    FakeIntegrationUnifi,
)


def _networks_by_name(unifi):
    return {net["name"]: net for net in NetworkConf(unifi, SITE).all()}


def test_network_list_is_hydrated_with_details():
    networks = _networks_by_name(FakeIntegrationUnifi())

    lan = networks["LAN"]
    assert lan["ipSubnet"] == "192.168.1.1/24"
    assert lan["gateway"] == "192.168.1.1"
    assert lan["dhcpdEnabled"] is True
    assert lan["dhcpdStart"] == "192.168.1.100"
    assert lan["dhcpdStop"] == "192.168.1.200"
    assert lan["dhcpdDns1"] == "1.1.1.1"
    assert lan["dhcpdDns2"] == "9.9.9.9"
    assert lan["vlanId"] == 1


def test_switch_managed_network_has_subnet_but_no_gateway():
    iot = _networks_by_name(FakeIntegrationUnifi())["IoT"]

    assert iot["ipSubnet"] == "10.20.0.2/24"
    assert "gateway" not in iot
    assert iot["dhcpdEnabled"] is False
    assert "dhcpdStart" not in iot


def test_unmanaged_and_unavailable_networks_keep_overview_fields():
    networks = _networks_by_name(FakeIntegrationUnifi())

    for name in ("Legacy", "Broken"):
        assert "ipSubnet" not in networks[name]
        assert networks[name]["vlanId"] in (30, 40)


def test_network_details_are_fetched_once_per_network():
    unifi = FakeIntegrationUnifi()
    conf = NetworkConf(unifi, SITE)

    conf.all()
    conf.all()

    assert len(unifi.detail_calls()) == len(NETWORK_OVERVIEWS)


def test_legacy_network_list_is_not_hydrated():
    class FakeLegacyUnifi:
        api_style = "legacy"

        def __init__(self):
            self.calls = []

        def make_request(self, endpoint, method="GET", data=None, params=None, **_kwargs):
            self.calls.append(endpoint)
            return {"meta": {"rc": "ok"}, "data": [{"_id": "a", "name": "LAN", "ip_subnet": "10.0.0.1/24"}]}

    unifi = FakeLegacyUnifi()
    networks = NetworkConf(unifi, SimpleNamespace(name="default", api_id="default")).all()

    assert networks == [{"_id": "a", "name": "LAN", "ip_subnet": "10.0.0.1/24"}]
    assert len(unifi.calls) == 1


def test_flatten_ignores_records_without_ipv4_configuration():
    assert flatten_integration_network({"id": "x", "management": "UNMANAGED"}) == {}


def test_hydrated_networks_yield_prefixes_and_dhcp_pool():
    main._unifi_network_info.clear()
    main._unifi_dhcp_ranges.clear()
    try:
        networks = _networks_by_name(FakeIntegrationUnifi())

        assert main._extract_prefix_cidr(networks["LAN"]) == "192.168.1.0/24"
        assert main._extract_prefix_cidr(networks["IoT"]) == "10.20.0.0/24"
        assert main._extract_prefix_cidr(networks["Legacy"]) is None

        site = SimpleNamespace(id="site-1", _id="site-1", network_conf=NetworkConf(FakeIntegrationUnifi(), SITE))
        pools = main.extract_dhcp_pools_from_unifi(site)

        assert len(pools) == 1
        pool = pools[0]
        assert str(pool["network"]) == "192.168.1.0/24"
        assert str(pool["start"]) == "192.168.1.100"
        assert str(pool["end"]) == "192.168.1.200"
        assert pool["gateway"] == "192.168.1.1"
        assert pool["dns"] == ["1.1.1.1", "9.9.9.9"]
    finally:
        main._unifi_network_info.clear()
        main._unifi_dhcp_ranges.clear()


def test_only_gateway_managed_networks_get_gateway_interfaces():
    networks = _networks_by_name(FakeIntegrationUnifi())

    assert main._is_gateway_managed_network(networks["LAN"]) is True
    assert main._is_gateway_managed_network(networks["IoT"]) is False
    assert main._is_gateway_managed_network(networks["Legacy"]) is False
    # Legacy networkconf records have no `management` field.
    assert main._is_gateway_managed_network({"name": "LAN", "ip_subnet": "10.0.0.1/24"}) is True


def test_port_poe_object_and_connector_types():
    ports = main.normalize_port_data(
        {
            "interfaces": {
                "ports": [
                    {
                        "idx": 1,
                        "connector": "RJ45",
                        "maxSpeedMbps": 2500,
                        "speedMbps": 1000,
                        "state": "UP",
                        "poe": {"enabled": True, "standard": "802.3at", "state": "UP", "type": 2},
                    },
                    {
                        "idx": 2,
                        "connector": "RJ45",
                        "maxSpeedMbps": 1000,
                        "state": "DOWN",
                        "poe": {"enabled": False, "standard": "802.3af", "state": "DOWN", "type": 1},
                    },
                    {"idx": 25, "connector": "SFPPLUS", "maxSpeedMbps": 10000, "state": "UP"},
                    {"idx": 26, "connector": "SFP28", "maxSpeedMbps": 25000, "state": "UP"},
                    {"idx": 27, "connector": "QSFP28", "maxSpeedMbps": 100000, "state": "UP"},
                ]
            }
        },
        api_style="integration",
    )
    by_name = {port["name"]: port for port in ports}

    assert by_name["Port 1"]["type"] == "2.5gbase-t"
    assert by_name["Port 1"]["poe_mode"] == "pse"
    assert by_name["Port 1"]["enabled"] is True
    assert by_name["Port 1"]["description"] == "PoE: 802.3at | Max speed: 2500Mbps"

    assert by_name["Port 2"]["poe_mode"] is None
    assert by_name["Port 2"]["enabled"] is False
    assert "PoE" not in by_name["Port 2"]["description"]

    assert by_name["Port 25"]["type"] == "10gbase-x-sfpp"
    assert by_name["Port 26"]["type"] == "25gbase-x-sfp28"
    assert by_name["Port 27"]["type"] == "100gbase-x-qsfp28"


def test_radios_without_name_are_named_by_frequency():
    radios = main.normalize_radio_data(
        {
            "interfaces": {
                "radios": [
                    {"frequencyGHz": 2.4, "wlanStandard": "802.11be", "channel": 6, "channelWidthMHz": 20},
                    {"frequencyGHz": 5, "wlanStandard": "802.11ac", "channelWidthMHz": 80},
                    {"frequencyGHz": 6, "wlanStandard": "802.11ax"},
                ]
            }
        },
        api_style="integration",
    )

    assert [radio["name"] for radio in radios] == ["radio-2.4GHz", "radio-5GHz", "radio-6GHz"]
    assert [radio["type"] for radio in radios] == ["ieee802.11ax", "ieee802.11ac", "ieee802.11ax"]
    assert all(radio["enabled"] is True for radio in radios)
    assert radios[0]["description"] == "Band: 2.4GHZ | Standard: 802.11be | Channel: 6 | Width: 20MHz"
    assert radios[1]["description"] == "Band: 5GHZ | Standard: 802.11ac | Width: 80MHz"


def _build_unifi(base_url):
    with patch.object(Unifi, "load_session_from_file", return_value=None), patch.object(
        Unifi, "configure_integration_api", return_value=True
    ), patch.object(Unifi, "get_sites", return_value={}):
        return Unifi(base_url, api_key="api-key")


def test_integration_base_candidates_cover_documented_server_urls():
    # https://{consoleIP}/proxy/network/integration  (server URL from the OpenAPI spec)
    assert _build_unifi("https://console.example.com/proxy/network/integration/")._integration_base_candidates() == [
        "https://console.example.com/proxy/network/integration/v1"
    ]
    assert _build_unifi("https://console.example.com/proxy/network/integration/v1")._integration_base_candidates() == [
        "https://console.example.com/proxy/network/integration/v1"
    ]
    assert _build_unifi("https://console.example.com")._integration_base_candidates() == [
        "https://console.example.com/proxy/network/integration/v1",
        "https://console.example.com/integration/v1",
    ]
