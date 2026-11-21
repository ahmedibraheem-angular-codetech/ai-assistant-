"""browser_read: public pages only, approval every time, private hops blocked."""

import asyncio
import os
from pathlib import Path

import pytest

import app.tools  # noqa: F401  (import order: registry before tool modules)
from app import browser
from app.browser import BrowserReadTool, UnsafeUrl, check_url, public_address
from app.permissions.standing import eligible
from app.tools.exceptions import ToolExecutionError
from app.tools.registry import get_tool_registry

PUBLIC = "93.184.215.14"


def fake_resolver(table):
    def resolve(host):
        if host not in table:
            raise UnsafeUrl("The site's address could not be found.")
        return table[host]

    return resolve


@pytest.mark.parametrize(
    "address,ok",
    [
        (PUBLIC, True),
        ("2606:4700:4700::1111", True),
        ("127.0.0.1", False),
        ("10.1.2.3", False),
        ("192.168.1.1", False),
        ("172.16.0.5", False),
        ("169.254.169.254", False),  # cloud metadata
        ("100.64.0.1", False),  # carrier-grade NAT
        ("::1", False),
        ("fe80::1", False),
        ("fd00::1", False),
        ("::ffff:127.0.0.1", False),
        ("0.0.0.0", False),
        ("224.0.0.1", False),
        ("not-an-ip", False),
    ],
)
def test_public_address(address, ok):
    assert public_address(address) is ok


def test_check_url_accepts_public_http_and_https():
    resolve = fake_resolver({"example.com": [PUBLIC]})
    assert check_url("https://example.com/a?b=1", resolve) == "https://example.com/a?b=1"
    assert check_url("http://example.com", resolve) == "http://example.com"


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/",
        "javascript:alert(1)",
        "chrome://settings",
        "https://user:pw@example.com/",
        "http://localhost:8000/api/v1/health",
        "http://app.localhost/",
        "http://printer.local/",
        "http://127.0.0.1:5678/webhook/x",
        "http://[::1]/",
        "http://10.0.0.1/",
        "http://2130706433/",  # 127.0.0.1 as an integer
        "https://mixed.example/",  # one public and one private address
        "https://nowhere.example/",
        "https://example.com/" + "a" * 2001,
    ],
)
def test_check_url_refuses_unsafe(url):
    resolve = fake_resolver({"example.com": [PUBLIC], "mixed.example": [PUBLIC, "10.0.0.7"]})
    with pytest.raises(UnsafeUrl):
        check_url(url, resolve)


def test_manifest_always_asks_and_no_standing_approval():
    tool = get_tool_registry().get("browser_read")
    assert tool is not None
    manifest = tool.permission_manifest
    assert manifest.approval_required and manifest.risk == "sensitive"
    assert not eligible(manifest)


def test_unsafe_url_is_refused_before_any_browser_starts(monkeypatch):
    def no_browser(*_args, **_kwargs):
        raise AssertionError("the browser must not start")

    monkeypatch.setattr(browser, "_read", no_browser)
    with pytest.raises(ToolExecutionError, match="public"):
        asyncio.run(BrowserReadTool().run({"url": "http://127.0.0.1:8000/"}))


# ---------------------------------------------------------------- real Chrome

CHROME = Path("/Applications/Google Chrome.app")
needs_chrome = pytest.mark.skipif(
    not CHROME.exists() or os.environ.get("BROWSER_TESTS") != "1",
    reason="set BROWSER_TESTS=1 (needs Google Chrome)",
)


SERVERS = r"""
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

class Secret(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
        self.wfile.write(b"<html><body>LOCAL-SECRET</body></html>")
    def log_message(self, *a): pass

secret = ThreadingHTTPServer(("127.0.0.1", 0), Secret)
target = f"http://127.0.0.1:{secret.server_port}/"

class Bouncer(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/page"):
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
            self.wfile.write(f'<html><body>public page<iframe src="{target}"></iframe></body></html>'.encode())
            return
        self.send_response(302); self.send_header("Location", target); self.send_header("Content-Length", "0"); self.end_headers()
    def log_message(self, *a): pass

bouncer = ThreadingHTTPServer(("127.0.0.1", 0), Bouncer)
for server in (secret, bouncer):
    threading.Thread(target=server.serve_forever, daemon=True).start()
print(bouncer.server_port, flush=True)
sys.stdin.read()
"""


@needs_chrome
def test_redirects_and_frames_to_private_addresses_are_blocked(monkeypatch, tmp_path):
    import subprocess
    import sys

    script = tmp_path / "servers.py"
    script.write_text(SERVERS)
    servers = subprocess.Popen([sys.executable, "-I", str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        port = int(servers.stdout.readline())
        # Only the bouncer (by name and port) may be reached on loopback; the secret
        # server it redirects to or frames is plain 127.0.0.1 and must be blocked.
        monkeypatch.setattr(browser, "resolve", fake_resolver({"bounce.localtest.me": [PUBLIC]}))
        monkeypatch.setattr(browser, "_TEST_ALLOW", {("bounce.localtest.me", port)})
        base = f"http://bounce.localtest.me:{port}"
        tool = BrowserReadTool()
        with pytest.raises(ToolExecutionError, match="non-public"):
            asyncio.run(tool.run({"url": f"{base}/go"}))

        page = asyncio.run(tool.run({"url": f"{base}/page"}))
        assert "public page" in page["text"]
        assert "LOCAL-SECRET" not in page["text"]
        assert any("127.0.0.1" in item for item in page["blocked_requests"])
        assert page["data_boundary"] == "untrusted_web_page"
    finally:
        servers.kill()
