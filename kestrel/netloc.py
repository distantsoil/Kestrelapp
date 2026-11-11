"""Where a model server address points: this computer, the local network, or the internet.

One classifier for every surface (consent, Hosted mode, labels, doctor). The address decides, never
a flag a settings file or a restored backup could carry (D-LLM-15). It uses NO DNS and no network:
only a LITERAL address decides.

- this computer: the name `localhost`, 127.0.0.0/8, ::1 (and IPv4-mapped 127.x).
- local network: literal RFC 1918 (10/8, 172.16/12, 192.168/16), IPv6 unique-local fc00::/7 and
  IPv6 link-local fe80::/10 addresses.
- internet: everything else, including EVERY other host name and every `*.local` name. A name can
  point anywhere and can change between a check and the connection, so a name is online.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

THIS_COMPUTER = "this_computer"
LOCAL_NETWORK = "local_network"
INTERNET = "internet"
_RANK = {THIS_COMPUTER: 0, LOCAL_NETWORK: 1, INTERNET: 2}


def host_of(url: str) -> str | None:
    """The lowercase host of URL (a bare host or an unbracketed IPv6 literal is accepted)."""
    text = (url or "").strip()
    if not text:
        return None
    if "://" not in text and "[" not in text and text.count(":") > 1:
        return text.lower().split("%", 1)[0]
    if "://" not in text:
        text = "//" + text
    try:
        host = urlsplit(text).hostname
    except ValueError:
        return None
    return host.lower().rstrip(".") if host else None


def classify_ip(ip_text: str) -> str:
    try:
        ip = ipaddress.ip_address(ip_text.split("%", 1)[0])
    except ValueError:
        return INTERNET
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_loopback:
        return THIS_COMPUTER
    if isinstance(ip, ipaddress.IPv4Address):
        for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"):
            if ip in ipaddress.ip_network(net):
                return LOCAL_NETWORK
        return INTERNET
    if ip in ipaddress.ip_network("fc00::/7") or ip in ipaddress.ip_network("fe80::/10"):
        return LOCAL_NETWORK
    return INTERNET


def classify_host(host: str | None) -> str:
    if not host:
        return INTERNET
    host = host.lower().rstrip(".")
    if host == "localhost":
        return THIS_COMPUTER
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return INTERNET  # a host name: never looked up, so never trusted
    return classify_ip(host)


def classify_url(url: str) -> str:
    """Where a call to URL goes. An empty URL (the in-process engine) stays on this computer."""
    text = (url or "").strip()
    if not text:
        return THIS_COMPUTER
    return classify_host(host_of(text))


def label_for(location: str, host: str = "") -> str:
    """The plain words for where a model runs."""
    if location == THIS_COMPUTER:
        return "on this computer"
    if location == LOCAL_NETWORK:
        return f"on another computer on your network ({host})" if host else (
            "on another computer on your network")
    return "online"
