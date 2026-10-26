"""Sync flows fed with UniFi Network API v10.6.106 payloads (networks, devices, clients)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from netbox_unifi.services import sync_engine
from netbox_unifi.services.unifi.networkconf import NetworkConf
from tests.integration_api_fixtures import AP_OVERVIEW, SITE, FakeIntegrationUnifi

NB_SITE = SimpleNamespace(id=5, name="HQ")
TENANT = SimpleNamespace(id=42)


class FakeRecordingEndpoint:
    """get() finds nothing; create() records the payload and returns it as an object."""

    def __init__(self, first_id=100):
        self.create_calls = []
        self._next_id = first_id

    def get(self, **_kwargs):
        return None

    def create(self, payload):
        self.create_calls.append(dict(payload))
        self._next_id += 1
        return SimpleNamespace(id=self._next_id, **payload)


class FakeVlanEndpoint(FakeRecordingEndpoint):
    def __init__(self, known_vids=()):
        super().__init__(first_id=100)
        self._known = {vid: SimpleNamespace(id=100 + vid, vid=vid) for vid in known_vids}

    def get(self, **kwargs):
        return self._known.get(kwargs.get("vid"))


class FakeInterfaceEndpoint(FakeRecordingEndpoint):
    def __init__(self):
        super().__init__(first_id=200)
        self.by_name = {}

    def get(self, **kwargs):
        return self.by_name.get(kwargs.get("name"))

    def filter(self, **_kwargs):
        return []

    def create(self, payload):
        created = super().create(payload)
        self.by_name[payload["name"]] = created
        return created


class FakeNetBox:
    def __init__(self, known_vids=(), prefixes=()):
        self.ipam = SimpleNamespace(
            vlan_groups=FakeRecordingEndpoint(first_id=8),
            vlans=FakeVlanEndpoint(known_vids),
            prefixes=FakePrefixEndpoint(prefixes),
            ip_addresses=FakeIPAddressEndpoint(),
        )
        self.dcim = SimpleNamespace(interfaces=FakeInterfaceEndpoint())


class FakePrefixEndpoint(FakeRecordingEndpoint):
    def __init__(self, existing=()):
        super().__init__(first_id=300)
        self._existing = list(existing)

    def filter(self, **_kwargs):
        return list(self._existing)


class FakeIPAddressObject(SimpleNamespace):
    def save(self):
        pass


class FakeIPAddressEndpoint(FakeRecordingEndpoint):
    def create(self, payload):
        self.create_calls.append(dict(payload))
        self._next_id += 1
        return FakeIPAddressObject(id=self._next_id, tags=[], **payload)


class FakeDevice:
    def __init__(self):
        self.id = 3
        self.primary_ip4 = None

    def save(self):
        pass


def _site_obj(unifi=None):
    return SimpleNamespace(
        name=SITE.name,
        api_id=SITE.api_id,
        network_conf=NetworkConf(unifi or FakeIntegrationUnifi(), SITE),
    )


@pytest.fixture(autouse=True)
def _clean_engine_caches():
    sync_engine._vlan_cache.clear()
    yield
    sync_engine._vlan_cache.clear()


def test_sync_site_vlans_creates_a_vlan_per_integration_network():
    nb = FakeNetBox()

    sync_engine.sync_site_vlans(nb, _site_obj(), NB_SITE, TENANT)

    created = {call["vid"]: call for call in nb.ipam.vlans.create_calls}
    assert {vid: call["name"] for vid, call in created.items()} == {
        1: "LAN",
        20: "IoT",
        30: "Legacy",
        40: "Broken",
    }
    assert all(call["status"] == "active" for call in created.values())
    assert all(call["site"] == NB_SITE.id and call["tenant"] == TENANT.id for call in created.values())


def test_sync_site_prefixes_creates_prefixes_linked_to_vlans_from_network_details():
    nb = FakeNetBox(known_vids=(1, 20, 30, 40))

    sync_engine.sync_site_prefixes(nb, _site_obj(), NB_SITE, TENANT, unifi=None)

    prefixes = {call["prefix"]: call for call in nb.ipam.prefixes.create_calls}
    # Overview-only networks (UNMANAGED, details unavailable) have no subnet and get no prefix.
    assert set(prefixes) == {"192.168.1.0/24", "10.20.0.0/24"}
    assert prefixes["192.168.1.0/24"] == {
        "prefix": "192.168.1.0/24",
        "status": "active",
        "tenant_id": TENANT.id,
        "description": "UniFi: LAN",
        "vlan": 101,
        "scope_type": "dcim.site",
        "scope_id": NB_SITE.id,
    }
    assert prefixes["10.20.0.0/24"]["vlan"] == 120
    assert prefixes["10.20.0.0/24"]["description"] == "UniFi: IoT"


def test_sync_gateway_interfaces_ignores_switch_managed_networks():
    nb = FakeNetBox()
    nb_device = FakeDevice()
    gateway = {"id": "gw-1", "name": "UCG-Max", "ipAddress": "192.168.1.1"}

    sync_engine.sync_gateway_interfaces(nb, nb_device, gateway, _site_obj(), TENANT, None, unifi=None)

    interfaces = nb.dcim.interfaces.create_calls
    assert [(call["name"], call["description"]) for call in interfaces] == [("vlan1", "LAN")]

    addresses = [call["address"] for call in nb.ipam.ip_addresses.create_calls]
    # 10.20.0.2 is the switch's address on the IoT network, not the gateway's.
    assert addresses == ["192.168.1.1/24"]
    assert nb_device.primary_ip4 is not None


def test_sync_device_interfaces_reads_ports_and_radios_from_device_details():
    nb = FakeNetBox()
    unifi = FakeIntegrationUnifi()

    sync_engine.sync_device_interfaces(
        nb,
        SimpleNamespace(id=3),
        dict(AP_OVERVIEW),
        "integration",
        unifi=unifi,
        site_obj=SITE,
    )

    created = {call["name"]: call for call in nb.dcim.interfaces.create_calls}
    assert list(created) == ["Port 1", "radio-2.4GHz", "radio-5GHz"]

    port = created["Port 1"]
    assert port["type"] == "2.5gbase-t"
    assert port["speed"] == 2_500_000
    assert port["poe_mode"] == "pse"
    assert port["description"] == "PoE: 802.3bt | Max speed: 2500Mbps"

    assert created["radio-2.4GHz"]["type"] == "ieee802.11ax"
    assert created["radio-2.4GHz"]["description"] == (
        "Band: 2.4GHZ | Standard: 802.11ax | Channel: 1 | Width: 20MHz"
    )
    assert created["radio-5GHz"]["description"] == (
        "Band: 5GHZ | Standard: 802.11ax | Channel: 36 | Width: 80MHz"
    )


def test_sync_client_ips_handles_integration_client_types(monkeypatch):
    monkeypatch.setenv("SYNC_CLIENT_IPS", "true")
    monkeypatch.setattr(sync_engine, "ensure_tag", lambda *args, **kwargs: SimpleNamespace(id=123))

    uplink = "66666666-6666-6666-6666-666666666666"
    clients = [
        {
            "id": "c1", "type": "WIRED", "name": "nas", "access": {"type": "DEFAULT"},
            "macAddress": "AA:BB:CC:00:00:10", "ipAddress": "192.168.1.20",
            "connectedAt": "2026-10-01T08:00:00Z", "uplinkDeviceId": uplink,
        },
        {
            "id": "c2", "type": "WIRELESS", "name": "phone", "access": {"type": "GUEST"},
            "macAddress": "aabbcc000011", "ipAddress": "192.168.1.21",
            "connectedAt": "2026-10-01T09:30:00Z", "uplinkDeviceId": uplink,
        },
        # VPN and Teleport clients carry no MAC address.
        {"id": "c3", "type": "VPN", "name": "road-warrior", "access": {"type": "DEFAULT"}, "ipAddress": "192.168.1.200"},
        # Connected but not yet leased an address.
        {"id": "c4", "type": "WIRED", "name": "no-ip", "access": {"type": "DEFAULT"}, "macAddress": "AA:BB:CC:00:00:12"},
    ]
    nb = FakeNetBox(prefixes=[SimpleNamespace(prefix="192.168.1.0/24")])
    site_obj = SimpleNamespace(client=SimpleNamespace(all=lambda: list(clients)))

    sync_engine.sync_client_ips(nb, site_obj, NB_SITE, TENANT)

    created = {call["address"]: call for call in nb.ipam.ip_addresses.create_calls}
    assert set(created) == {"192.168.1.20/24", "192.168.1.21/24"}
    assert created["192.168.1.20/24"]["status"] == "dhcp"
    assert created["192.168.1.20/24"]["description"] == (
        "unifi-client:AA:BB:CC:00:00:10 | UniFi client: nas | IP: 192.168.1.20 | "
        "Connected: 2026-10-01T08:00:00Z"
    )
    # The 12-hex MAC is normalised and the missing lastSeen does not make the client look stale.
    assert created["192.168.1.21/24"]["description"].startswith("unifi-client:AA:BB:CC:00:00:11 | ")


@pytest.mark.parametrize(
    ("device", "expected"),
    [
        ({"model": "USW-Pro-24-PoE", "features": ["switching"], "interfaces": ["ports"]}, "LAN"),
        ({"model": "U7 Pro", "features": ["accessPoint"], "interfaces": ["ports", "radios"]}, "WIRELESS"),
        ({"model": "X1", "features": ["gateway", "switching"], "interfaces": ["ports"]}, "GATEWAY"),
    ],
)
def test_role_is_inferred_from_integration_feature_lists(device, expected):
    assert sync_engine.infer_role_key_for_device(device) == expected


def test_device_overview_interfaces_are_metadata_not_ports_or_radios():
    # GET /devices returns interfaces as ["ports", "radios"]; objects only exist on GET /devices/{id}.
    assert sync_engine.normalize_port_data(AP_OVERVIEW, "integration") == []
    assert sync_engine.normalize_radio_data(AP_OVERVIEW, "integration") == []
    assert sync_engine.is_access_point_device(AP_OVERVIEW) is True
