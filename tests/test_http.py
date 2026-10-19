import http.server
import threading
import urllib.error

import pytest

from aggregator.sources import http as aggregator_http


@pytest.fixture
def server():
    """A local HTTP server serving a few fixed routes."""
    routes = {
        "/feed": (200, b"<rss/>"),
        "/missing": (404, b"nope"),
        "/big": (200, b"x" * 5000),
    }
    seen_headers = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen_headers.update(self.headers)
            status, body = routes.get(self.path, (500, b""))
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}", seen_headers
    httpd.shutdown()
    httpd.server_close()


def test_get_returns_the_body_and_sends_a_user_agent(server):
    base, headers = server
    assert aggregator_http.get(f"{base}/feed") == b"<rss/>"
    assert headers["User-Agent"].startswith("news-aggregator/")


def test_get_raises_on_http_errors(server):
    base, _ = server
    with pytest.raises(urllib.error.HTTPError):
        aggregator_http.get(f"{base}/missing")


def test_get_enforces_the_size_limit(server):
    base, _ = server
    with pytest.raises(aggregator_http.TooLarge):
        aggregator_http.get(f"{base}/big", max_bytes=1000)
