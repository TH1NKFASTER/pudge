"""Which of this Mac's addresses the iPhone/iPad service uses: private LAN interfaces only.

VPN tunnels (utun, ipsec, ppp ...) and public addresses are never put in the pairing QR,
never accept phone connections and are never advertised over Bonjour.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import subprocess
import sys
import threading
import time

_PRIVATE = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "fc00::/7", "fe80::/10")
)
# Tunnels and other virtual interfaces a phone on the same Wi-Fi/Ethernet never reaches.
_VIRTUAL = ("lo", "utun", "ipsec", "ppp", "tun", "tap", "gif", "stf", "awdl", "llw", "anpi", "vmenet", "wg")
_cache_lock = threading.Lock()
_cache: tuple[float, frozenset[str]] = (-100.0, frozenset())


def is_private(address: str) -> bool:
    """RFC 1918, link-local or IPv6 unique-local (fc00::/7) / link-local address."""
    try:
        value = ipaddress.ip_address(str(address).split("%", 1)[0])
    except ValueError:
        return False
    return any(value.version == network.version and value in network for network in _PRIVATE)


def is_lan_interface(name: str) -> bool:
    return bool(name) and not str(name).startswith(_VIRTUAL)


def interface_addresses() -> list[tuple[str, str]]:
    """(interface, IPv4 address) of the up interfaces; ("", address) where names are unknown."""
    if sys.platform == "darwin":
        try:
            text = subprocess.run(
                ["/sbin/ifconfig"], capture_output=True, text=True, timeout=5, check=False
            ).stdout
        except (OSError, subprocess.SubprocessError):
            text = ""
        result: list[tuple[str, str]] = []
        name, up = "", False
        for line in text.splitlines():
            header = re.match(r"^([A-Za-z0-9_.-]+): flags=\w+<([^>]*)>", line)
            if header:
                name, up = header[1], "UP" in header[2].split(",")
                continue
            match = re.match(r"^\s+inet (\d+\.\d+\.\d+\.\d+)\b", line)
            if match and name and up:
                result.append((name, match[1]))
        if result:
            return result
    # Elsewhere (and if ifconfig fails): the addresses the system reports for this host.
    found: list[tuple[str, str]] = []
    route = default_route_address()
    if route:
        found.append(("", route))
    try:
        for item in socket.getaddrinfo(
            socket.gethostname(), None, family=socket.AF_INET, type=socket.SOCK_STREAM
        ):
            found.append(("", str(item[4][0])))
    except OSError:
        pass
    return found


def default_route_address() -> str:
    """Source address of the default route (a VPN's tunnel address when a full-tunnel VPN is on)."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("1.1.1.1", 80))
            return str(sock.getsockname()[0])
        finally:
            sock.close()
    except OSError:
        return ""


def lan_interfaces() -> list[tuple[str, str]]:
    """(interface, address) pairs a phone on the local network can reach, best first.

    The interface of the default route comes first when it is a LAN interface (not a VPN);
    then Ethernet/Wi-Fi (en0, en1 ...), then other interfaces; link-local addresses last.
    """
    seen: set[str] = set()
    pairs: list[tuple[str, str]] = []
    for name, address in interface_addresses():
        if address in seen or not is_private(address) or (name and not is_lan_interface(name)):
            continue
        seen.add(address)
        pairs.append((name, address))
    route = default_route_address()

    def rank(item: tuple[str, str]) -> tuple[int, int, int, int, str]:
        name, address = item
        number = re.search(r"(\d+)$", name)
        return (
            1 if address.startswith("169.254.") else 0,
            0 if address == route else 1,
            0 if name.startswith("en") else 1,
            int(number[1]) if number else 0,
            name,
        )

    return sorted(pairs, key=rank)


def lan_hosts(limit: int = 4) -> list[str]:
    """Addresses for the pairing QR, best first (at most `limit`)."""
    return [address for _name, address in lan_interfaces()][: max(1, limit)]


def lan_addresses(max_age: float = 10.0) -> frozenset[str]:
    """This Mac's private LAN addresses (cached for a few seconds: checked on every connection)."""
    global _cache
    with _cache_lock:
        at, value = _cache
        if time.monotonic() - at <= max_age:
            return value
    value = frozenset(address for _name, address in lan_interfaces())
    with _cache_lock:
        _cache = (time.monotonic(), value)
    return value


def accepts(local_address: str, peer_address: str) -> bool:
    """A phone connection is served only on a private LAN address of this Mac, from a private peer.

    Loopback (the iOS Simulator on this Mac) is allowed; connections that arrive through a VPN
    tunnel's address or a public address are closed before TLS.
    """
    local, peer = str(local_address or ""), str(peer_address or "")
    if local.startswith("::ffff:"):
        local = local[7:]
    if peer.startswith("::ffff:"):
        peer = peer[7:]
    try:
        if ipaddress.ip_address(local.split("%", 1)[0]).is_loopback:
            return ipaddress.ip_address(peer.split("%", 1)[0]).is_loopback
    except ValueError:
        return False
    return is_private(peer) and local in lan_addresses()
