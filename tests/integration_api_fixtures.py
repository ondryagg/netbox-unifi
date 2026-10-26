"""Shared payload fixtures shaped after the UniFi Network API v10.6.106 OpenAPI spec."""

from types import SimpleNamespace

SITE = SimpleNamespace(api_id="site-1", name="Default", id="site-1", _id="site-1")

LAN_ID = "11111111-1111-1111-1111-111111111111"
IOT_ID = "22222222-2222-2222-2222-222222222222"
UNMANAGED_ID = "33333333-3333-3333-3333-333333333333"
BROKEN_ID = "44444444-4444-4444-4444-444444444444"

NETWORK_OVERVIEWS = [
    {"id": LAN_ID, "name": "LAN", "vlanId": 1, "management": "GATEWAY", "enabled": True, "default": True},
    {"id": IOT_ID, "name": "IoT", "vlanId": 20, "management": "SWITCH", "enabled": True, "default": False},
    {"id": UNMANAGED_ID, "name": "Legacy", "vlanId": 30, "management": "UNMANAGED", "enabled": True, "default": False},
    {"id": BROKEN_ID, "name": "Broken", "vlanId": 40, "management": "GATEWAY", "enabled": True, "default": False},
]

NETWORK_DETAILS = {
    LAN_ID: {
        **NETWORK_OVERVIEWS[0],
        "ipv4Configuration": {
            "hostIpAddress": "192.168.1.1",
            "prefixLength": 24,
            "autoScaleEnabled": False,
            "dhcpConfiguration": {
                "mode": "SERVER",
                "ipAddressRange": {"start": "192.168.1.100", "stop": "192.168.1.200"},
                "dnsServerIpAddressesOverride": ["1.1.1.1", "9.9.9.9"],
                "leaseTimeSeconds": 86400,
            },
        },
    },
    IOT_ID: {
        **NETWORK_OVERVIEWS[1],
        "ipv4Configuration": {
            "hostIpAddress": "10.20.0.2",
            "prefixLength": 24,
            "autoScaleEnabled": False,
            "dhcpConfiguration": {"mode": "RELAY", "dhcpServerIpAddresses": ["192.168.1.1"]},
        },
    },
    UNMANAGED_ID: dict(NETWORK_OVERVIEWS[2]),
}


AP_ID = "55555555-5555-5555-5555-555555555555"

# GET /devices lists interfaces as plain strings; the port/radio objects are on GET /devices/{id}.
AP_OVERVIEW = {
    "id": AP_ID,
    "name": "AP-Lobby",
    "model": "U7 Pro",
    "macAddress": "aa:bb:cc:00:00:01",
    "ipAddress": "192.168.1.50",
    "state": "ONLINE",
    "firmwareVersion": "8.0.7",
    "firmwareUpdatable": False,
    "supported": True,
    "features": ["accessPoint"],
    "interfaces": ["ports", "radios"],
}

DEVICE_DETAILS = {
    AP_ID: {
        **AP_OVERVIEW,
        "configurationId": "cfg-1",
        "interfaces": {
            "ports": [
                {
                    "idx": 1,
                    "connector": "RJ45",
                    "maxSpeedMbps": 2500,
                    "speedMbps": 1000,
                    "state": "UP",
                    "poe": {"enabled": True, "standard": "802.3bt", "state": "UP", "type": 3},
                }
            ],
            "radios": [
                {"frequencyGHz": 2.4, "wlanStandard": "802.11ax", "channel": 1, "channelWidthMHz": 20},
                {"frequencyGHz": 5, "wlanStandard": "802.11ax", "channel": 36, "channelWidthMHz": 80},
            ],
        },
    }
}


class FakeIntegrationUnifi:
    api_style = "integration"

    def __init__(self):
        self.calls = []

    def make_request(self, endpoint, method="GET", data=None, params=None, **_kwargs):
        self.calls.append(endpoint)
        if endpoint == "/sites/site-1/networks":
            return {
                "offset": 0,
                "limit": 200,
                "count": len(NETWORK_OVERVIEWS),
                "totalCount": len(NETWORK_OVERVIEWS),
                "data": list(NETWORK_OVERVIEWS),
            }
        resource_id = endpoint.rsplit("/", 1)[-1]
        if endpoint.startswith("/sites/site-1/devices/") and resource_id in DEVICE_DETAILS:
            return dict(DEVICE_DETAILS[resource_id])
        if endpoint.startswith("/sites/site-1/networks/") and resource_id in NETWORK_DETAILS:
            return dict(NETWORK_DETAILS[resource_id])
        return {"statusCode": 404, "code": "api.err.not-found", "message": "Not found"}

    def detail_calls(self):
        """Per-network detail lookups (everything but the list call)."""
        return [call for call in self.calls if "/networks/" in call]
