"""Tests for the read_url tool — SSRF defenses (scheme allowlist, private/metadata
IP screening incl. IPv4-mapped IPv6, resolve-once IP PINNING so DNS rebinding
can't reach internal hosts, per-redirect re-validation, domain allowlist) plus
HTML->Markdown, size/char caps, content-type filtering, and graceful errors.
All network + DNS is mocked, so these run fully offline.

Runs standalone (``python test_url_reader.py``) or under pytest.
"""

from __future__ import annotations

import asyncio
import ipaddress

import httpx

import v1.core.tools.url_reader.url_reader as m

# The tool ships disabled by default; enable it for the fetch tests.
m.settings.url_reader_enabled = True


# --- fakes -------------------------------------------------------------------

def _install_dns(mapping):
    """Patch socket.getaddrinfo; mapping host->ip (or None to fail). IP literals
    resolve to themselves (as real getaddrinfo does)."""
    orig = m.socket.getaddrinfo

    def fake(host, *args, **kwargs):
        try:
            ipaddress.ip_address(host)
            return [(2, 1, 6, "", (host, 0))]
        except ValueError:
            pass
        ip = mapping(host) if callable(mapping) else mapping.get(host, "93.184.216.34")
        if ip is None:
            raise m.socket.gaierror(f"cannot resolve {host}")
        return [(2, 1, 6, "", (ip, 0))]

    m.socket.getaddrinfo = fake
    return lambda: setattr(m.socket, "getaddrinfo", orig)


class _FakeResp:
    def __init__(self, status_code=200, headers=None, body=b"", is_redirect=False):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body
        self.is_redirect = is_redirect

    async def aiter_raw(self):
        b = self._body
        if not b:
            return
        step = max(1, len(b) // 3)
        for i in range(0, len(b), step):
            yield b[i:i + step]


class _FakeStream:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


def _install_httpx(responder):
    """Patch httpx.AsyncClient; responder(url_str, index) -> _FakeResp or raises.
    Records each stream call's url/headers/extensions in the returned list."""
    orig = m.httpx.AsyncClient
    calls: list[dict] = []

    class _FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, headers=None, extensions=None):
            idx = len(calls)
            calls.append({"url": str(url), "headers": headers or {}, "extensions": extensions or {}})
            return _FakeStream(responder(str(url), idx))

    m.httpx.AsyncClient = _FakeClient
    return calls, (lambda: setattr(m.httpx, "AsyncClient", orig))


def _read(url):
    return asyncio.run(m.read_url_tool.ainvoke({"url": url}))


def _html(body_html):
    return _FakeResp(200, {"content-type": "text/html; charset=utf-8"}, body_html.encode())


# --- static validation + IP screening ---------------------------------------

def test_blocks_non_http_schemes() -> None:
    for bad in ["file:///etc/passwd", "ftp://x/y", "gopher://x", "data:text/html,hi",
                "javascript:alert(1)"]:
        out = m._validate_static(bad)
        assert out and "http and https" in out, bad


def test_ip_screen_blocks_private_metadata_and_mapped() -> None:
    for ip in ["169.254.169.254", "127.0.0.1", "10.1.2.3", "192.168.0.5", "::1",
               "::ffff:169.254.169.254", "0.0.0.0", "fe80::1"]:
        assert m._ip_is_blocked(ip) is True, ip
    for ip in ["93.184.216.34", "8.8.8.8", "1.1.1.1"]:
        assert m._ip_is_blocked(ip) is False, ip


def test_read_blocks_internal_hosts_end_to_end() -> None:
    blocked = {"meta.evil": "169.254.169.254", "lo.evil": "127.0.0.1",
               "rfc1918.evil": "10.1.2.3", "lan.evil": "192.168.0.5"}
    restore_dns = _install_dns(blocked)
    calls, restore_httpx = _install_httpx(lambda u, i: _html("<html>x</html>"))
    try:
        for host in blocked:
            out = _read(f"http://{host}/x")
            assert "internal" in out.lower(), host
        assert "internal" in _read("http://169.254.169.254/latest/meta-data/").lower()
        assert calls == []  # a blocked host is NEVER fetched
    finally:
        restore_httpx()
        restore_dns()


def test_domain_allowlist() -> None:
    orig = m.settings.url_reader_allowed_domains
    m.settings.url_reader_allowed_domains = ["example.com"]
    try:
        assert m._validate_static("https://evil.com/x") is not None       # not allowed
        assert m._validate_static("https://docs.example.com/x") is None    # subdomain allowed
        assert m._validate_static("https://example.com/x") is None
        assert m._validate_static("https://notexample.com/x") is not None  # suffix must be dot-bounded
    finally:
        m.settings.url_reader_allowed_domains = orig


# --- IP pinning (DNS-rebinding defense) --------------------------------------

def test_connects_to_vetted_ip_not_hostname() -> None:
    # The rebinding fix: we resolve+screen ONCE and connect to that IP, keeping
    # Host + SNI as the name. So httpx is handed the IP, never the hostname (a
    # second resolution can't swing it to an internal address).
    restore_dns = _install_dns({"example.com": "93.184.216.34"})
    calls, restore_httpx = _install_httpx(lambda u, i: _html("<html><body>ok</body></html>"))
    try:
        _read("https://example.com/page")
        assert len(calls) == 1
        assert httpx.URL(calls[0]["url"]).host == "93.184.216.34"      # connected to the vetted IP
        assert calls[0]["headers"].get("Host") == "example.com"        # vhost preserved
        assert calls[0]["extensions"].get("sni_hostname") == "example.com"  # TLS SNI preserved
    finally:
        restore_httpx()
        restore_dns()


def test_redirect_to_internal_is_blocked() -> None:
    restore_dns = _install_dns({"public.example": "93.184.216.34", "internal.evil": "169.254.169.254"})

    def responder(url, idx):
        if idx == 0:
            return _FakeResp(302, {"location": "http://internal.evil/latest/meta-data/"}, is_redirect=True)
        return _html("<html>should never reach here</html>")

    calls, restore_httpx = _install_httpx(responder)
    try:
        out = _read("https://public.example/start")
        assert "internal" in out.lower()
        # only the public host was ever connected to; the internal hop was screened
        assert len(calls) == 1
        assert httpx.URL(calls[0]["url"]).host == "93.184.216.34"
    finally:
        restore_httpx()
        restore_dns()


# --- fetch behaviour ---------------------------------------------------------

def test_html_becomes_markdown_without_scripts() -> None:
    restore_dns = _install_dns(lambda h: "93.184.216.34")
    html = "<html><body><h1>Title</h1><p>Hello <b>world</b></p><script>steal()</script></body></html>"
    calls, restore_httpx = _install_httpx(lambda u, i: _html(html))
    try:
        out = _read("https://example.com/page")
        assert "# Title" in out and "Hello" in out and "world" in out
        assert "steal()" not in out  # script content removed
    finally:
        restore_httpx()
        restore_dns()


def test_size_and_char_caps() -> None:
    restore_dns = _install_dns(lambda h: "93.184.216.34")
    big = "<html><body>" + ("A" * 100000) + "</body></html>"
    calls, restore_httpx = _install_httpx(lambda u, i: _html(big))
    ob, oc = m.settings.url_reader_max_bytes, m.settings.url_reader_max_chars
    m.settings.url_reader_max_bytes, m.settings.url_reader_max_chars = 5000, 500
    try:
        out = _read("https://example.com/big")
        assert "[... content truncated ...]" in out
        assert len(out) <= 500 + len("\n\n[... content truncated ...]") + 5
    finally:
        m.settings.url_reader_max_bytes, m.settings.url_reader_max_chars = ob, oc
        restore_httpx()
        restore_dns()


def test_rejects_binary_content_type() -> None:
    restore_dns = _install_dns(lambda h: "93.184.216.34")
    resp = _FakeResp(200, {"content-type": "application/pdf"}, b"%PDF-1.7 ...")
    calls, restore_httpx = _install_httpx(lambda u, i: resp)
    try:
        assert "readable web page" in _read("https://example.com/file.pdf")
    finally:
        restore_httpx()
        restore_dns()


def test_http_error_and_timeout_are_graceful() -> None:
    restore_dns = _install_dns(lambda h: "93.184.216.34")
    calls, restore_httpx = _install_httpx(lambda u, i: _FakeResp(404, {"content-type": "text/html"}, b"no"))
    try:
        assert "could not be fetched" in _read("https://example.com/missing").lower()
    finally:
        restore_httpx()
    def boom(u, i):
        raise httpx.TimeoutException("slow")
    calls, restore_httpx = _install_httpx(boom)
    try:
        assert "too long" in _read("https://example.com/slow").lower()
    finally:
        restore_httpx()
        restore_dns()


def test_disabled_and_empty() -> None:
    orig = m.settings.url_reader_enabled
    m.settings.url_reader_enabled = False
    try:
        assert "not available" in _read("https://example.com").lower()
    finally:
        m.settings.url_reader_enabled = orig
    assert "no url" in _read("   ").lower()


def _main() -> int:
    checks = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failures = 0
    for check in checks:
        try:
            check()
        except Exception as exc:  # noqa: BLE001 - standalone runner reports all
            failures += 1
            print(f"FAIL {check.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {check.__name__}")
    print(f"\n{len(checks) - failures}/{len(checks)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())
