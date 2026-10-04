from types import SimpleNamespace

import httpx
import pytest

from network_isolation import install


@pytest.fixture
def boundary():
    calls = []
    class Socket:
        family = 2
        def connect(self, address): calls.append(('connect', address))
        def connect_ex(self, address): calls.append(('connect_ex', address)); return 0
        def sendto(self, *args): calls.append(('sendto', args)); return len(args[0])
    def lookup(host, *args): calls.append(('lookup', host)); return ['local result']
    module = SimpleNamespace(socket=Socket, AF_UNIX=1, getaddrinfo=lookup,
                             gethostbyname=lookup, gethostbyname_ex=lookup, gethostbyaddr=lookup)
    install(module); install(module)
    return module, calls


@pytest.mark.parametrize('method', ['getaddrinfo', 'gethostbyname', 'gethostbyname_ex', 'gethostbyaddr'])
def test_external_dns_is_blocked_before_original_function(boundary, method):
    module, calls = boundary
    with pytest.raises(OSError, match='disabled'):
        getattr(module, method)('example.com')
    assert calls == []
    getattr(module, method)('127.0.0.1')
    assert calls == [('lookup', '127.0.0.1')]


@pytest.mark.parametrize('method', ['connect', 'connect_ex', 'sendto'])
def test_external_ip_cannot_bypass_offline_boundary(boundary, method):
    module, calls = boundary
    sock = module.socket()
    args = (b'synthetic bytes', ('192.0.2.1', 443)) if method == 'sendto' else (('192.0.2.1', 443),)
    with pytest.raises(OSError, match='disabled'):
        getattr(sock, method)(*args)
    assert calls == []
    local_args = (b'synthetic bytes', ('::1', 9000)) if method == 'sendto' else (('::1', 9000),)
    getattr(sock, method)(*local_args)
    assert len(calls) == 1
    sock.family = module.AF_UNIX
    if method != 'sendto':
        getattr(sock, method)('/tmp/synthetic-test-socket')
        assert len(calls) == 2


def test_offline_boundary_keeps_http_mock_transport_usable():
    seen = []
    def reply(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={'synthetic': True})
    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        assert client.get('https://example.com/fixture').json() == {'synthetic': True}
    assert seen == ['https://example.com/fixture']


@pytest.mark.parametrize('style', ['post', 'keyword_request', 'module_get'])
def test_external_http_is_blocked_before_transport_receives_request(monkeypatch, style):
    from network_isolation import install_http
    install_http()
    called = []
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', lambda *args: called.append(args))
    with httpx.Client(trust_env=False) as client:
        with pytest.raises(httpx.ConnectError, match='disabled'):
            if style == 'post':
                client.post('https://example.com/offline-fixture', json={'synthetic': True})
            elif style == 'keyword_request':
                client.request(method='GET', url='https://example.com/offline-fixture')
            else:
                httpx.get('https://example.com/offline-fixture', trust_env=False)
    assert called == []
