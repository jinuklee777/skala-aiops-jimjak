"""테스트용 가짜 서버, 시계, 표본 응답."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from collector.config import KST, Config

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SERVICE_KEY = "test+Key/abc=="  # 포털의 인코딩 안 된 인증키처럼 +, /, = 가 들어 있다


def fixture(name) -> bytes:
    return (FIXTURES / name).read_bytes()


def make_config(data_dir, server_url, **overrides) -> Config:
    # 10분 간격으로 두어 자정 앞뒤로 촘촘해지는 구간까지 시험한다.
    values = dict(service_key=SERVICE_KEY, data_dir=Path(data_dir),
                  base_url=f"{server_url}/B551177/statusOfBaggageClaimDesk",
                  interval_minutes=10, num_of_rows=3, max_attempts=2, timeout_seconds=5)
    values.update(overrides)
    return Config(**values)


class FakeClock:
    def __init__(self, *moment):
        self.now = datetime(*moment, tzinfo=KST)

    def __call__(self):
        return self.now

    def set(self, *moment):
        self.now = datetime(*moment, tzinfo=KST)

    def advance(self, **delta):
        self.now += timedelta(**delta)


class FakeServer:
    """요청마다 handler(method, path, query, body) → (상태, 본문)을 돌려주는 로컬 HTTP 서버.

    requests에는 (method, path, 원래 query 문자열, body)가 쌓인다.
    """

    def __init__(self, handler):
        self.handler = handler
        self.requests = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.reply()

            def do_POST(self):
                self.reply()

            def reply(self):
                url = urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                server.requests.append((self.command, url.path, url.query, body))
                status, payload = server.handler(self.command, url.path, dict(parse_qsl(url.query)), body)
                self.send_response(status)
                self.send_header("Content-Type", "application/json;charset=UTF-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
