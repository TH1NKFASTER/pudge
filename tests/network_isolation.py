"""Disable real external DNS and connections in the source test process.

HTTP MockTransport and local test servers remain usable. This module is never
loaded by Pudge itself; production connections and account settings are intact.
"""
from __future__ import annotations

import errno
import ipaddress
import socket

import httpx


def _local(host) -> bool:
    if host is None or host in ('', 'localhost', b'localhost'):
        return True
    try:
        text = host.decode('ascii') if isinstance(host, bytes) else str(host)
        return ipaddress.ip_address(text).is_loopback
    except (UnicodeError, ValueError):
        return False


def install(module=socket) -> None:
    if getattr(module, '_pudge_offline_tests', False):
        return
    module._pudge_offline_tests = True

    def require_local(host):
        if not _local(host):
            raise OSError(errno.ENETUNREACH, 'External network is disabled in Pudge tests')

    for name in ('getaddrinfo', 'gethostbyname', 'gethostbyname_ex', 'gethostbyaddr'):
        original = getattr(module, name)
        def lookup(host, *args, _original=original, **kwargs):
            require_local(host)
            return _original(host, *args, **kwargs)
        setattr(module, name, lookup)

    for name in ('connect', 'connect_ex', 'sendto'):
        original = getattr(module.socket, name)
        def send(self, *args, _original=original, **kwargs):
            if self.family != module.AF_UNIX:
                address = args[-1]
                require_local(address[0])
            return _original(self, *args, **kwargs)
        setattr(module.socket, name, send)


def install_http() -> None:
    """Reject real HTTP requests before URL, headers or body reach a transport."""
    if getattr(httpx.Client, '_pudge_offline_tests', False):
        return
    httpx.Client._pudge_offline_tests = True

    for name in ('request', 'send'):
        original = getattr(httpx.Client, name)
        def request(self, *args, _original=original, _name=name, **kwargs):
            if _name == 'send':
                value = args[0] if args else kwargs['request']
                url = value.url
            else:
                url = args[1] if len(args) > 1 else kwargs.get('url', '')
            target = httpx.URL(url)
            if not target.is_absolute_url:
                target = self.base_url.join(target)
            transport = self._transport_for_url(target)
            if not _local(target.host) and isinstance(transport, httpx.HTTPTransport):
                raise httpx.ConnectError('External HTTP is disabled in Pudge tests')
            return _original(self, *args, **kwargs)
        setattr(httpx.Client, name, request)
