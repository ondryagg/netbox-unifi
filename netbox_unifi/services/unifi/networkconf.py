from .resources import BaseResource
import logging
logger = logging.getLogger(__name__)


def flatten_integration_network(net):
    """Map Integration API ``ipv4Configuration`` onto the flat keys the sync code reads.

    From v10.x the network list only carries id/name/vlanId/management; addressing
    and DHCP live under ``ipv4Configuration`` on the per-network details response.
    Returns only the keys that can be derived, so callers can merge the result over
    the raw record.
    """
    ipv4 = net.get("ipv4Configuration")
    if not isinstance(ipv4, dict):
        return {}

    flat = {}
    host_ip = ipv4.get("hostIpAddress")
    prefix_length = ipv4.get("prefixLength")
    if host_ip and prefix_length is not None:
        flat["ipSubnet"] = f"{host_ip}/{prefix_length}"
        # On switch-managed networks hostIpAddress is the switch's address, not a gateway's.
        if str(net.get("management") or "").upper() == "GATEWAY":
            flat["gateway"] = host_ip

    dhcp = ipv4.get("dhcpConfiguration")
    if isinstance(dhcp, dict):
        is_server = str(dhcp.get("mode") or "").upper() == "SERVER"
        flat["dhcpdEnabled"] = is_server
        if is_server:
            ip_range = dhcp.get("ipAddressRange")
            if isinstance(ip_range, dict):
                flat["dhcpdStart"] = ip_range.get("start")
                flat["dhcpdStop"] = ip_range.get("stop")
            for index, dns in enumerate(dhcp.get("dnsServerIpAddressesOverride") or [], start=1):
                flat[f"dhcpdDns{index}"] = dns
    return flat


class NetworkConf(BaseResource):
    BASE_PATH = 'rest'
    API_PATH = "/api/s"
    INTEGRATION_API_PATH = "/sites"
    INTEGRATION_ENDPOINT = "networks"

    def __init__(self, unifi, site, **kwargs):
        self.unifi = unifi
        self.site = site
        self._detail_cache = {}
        if getattr(unifi, "api_style", None) == "integration":
            super().__init__(
                unifi,
                site,
                endpoint=self.INTEGRATION_ENDPOINT,
                api_path=self.INTEGRATION_API_PATH,
                base_path=None,
                **kwargs,
            )
        else:
            super().__init__(unifi, site, endpoint='networkconf', api_path=self.API_PATH, base_path=self.BASE_PATH, **kwargs)

    def all(self, filter_query=None, limit=200) -> list:
        networks = super().all(filter_query=filter_query, limit=limit)
        if getattr(self.unifi, "api_style", None) != "integration":
            return networks
        return [self._with_details(net) for net in networks]

    def _with_details(self, net):
        """Merge GET /networks/{id} into a list entry and add the flat addressing keys."""
        net_id = net.get("id")
        if not net_id:
            return net

        cached = self._detail_cache.get(net_id)
        if cached is None:
            response = self.unifi.make_request(self._build_url(item_id=net_id), "GET")
            detail = self._extract_response_data(response)
            if isinstance(detail, dict):
                cached = {**net, **detail}
                cached.update(flatten_integration_network(cached))
            else:
                logger.debug(
                    f"No details for network {net.get('name') or net_id}: "
                    f"{self._response_error_message(response)}"
                )
                cached = net
            # A failed lookup is cached too so one bad record isn't retried on every .all().
            self._detail_cache[net_id] = cached
        return dict(cached)
