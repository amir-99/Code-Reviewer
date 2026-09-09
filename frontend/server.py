"""Serve the dashboard and stream requests to a fixed operator-configured API."""

import http.client
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).parent
UPSTREAM = urlsplit(os.environ.get("REVIEWER_API_URL", "http://api:8080"))
ASSETS = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
          "/sse.js": ("sse.js", "text/javascript"), "/flow.js": ("flow.js", "text/javascript"),
          "/models.js": ("models.js", "text/javascript"),
          "/spend.js": ("spend.js", "text/javascript"),
          "/style.css": ("style.css", "text/css"),
          "/favicon.svg": ("favicon.svg", "image/svg+xml")}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # Never log credentials, request URLs, or upstream bodies.

    def headers_for(self, status, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/api/admin/"):
            return self.proxy()
        asset = ASSETS.get(self.path)
        if not asset:
            self.send_error(404)
            return
        name, mime = asset
        self.headers_for(200, mime)
        self.wfile.write((ROOT / name).read_bytes())

    def do_POST(self):
        if self.path.startswith("/api/admin/"):
            return self.proxy()
        self.send_error(404)

    def proxy(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400)
            return
        if not 0 <= size <= 65536 or self.headers.get("Transfer-Encoding"):
            self.send_error(413)
            return
        self.connection.settimeout(45)
        connection_type = http.client.HTTPSConnection if UPSTREAM.scheme == "https" else http.client.HTTPConnection
        connection = connection_type(UPSTREAM.hostname, UPSTREAM.port, timeout=45)
        sent = False
        try:
            headers = {k: self.headers[k] for k in ("Authorization", "Content-Type", "Last-Event-ID", "Accept") if k in self.headers}
            connection.request(self.command, UPSTREAM.path.rstrip("/") + self.path[4:], self.rfile.read(size) if size else None, headers)
            response = connection.getresponse()
            self.headers_for(response.status, response.getheader("Content-Type", "application/json"))
            sent = True
            while chunk := response.read1(16384):
                self.wfile.write(chunk)
                self.wfile.flush()
        except (OSError, http.client.HTTPException):
            if not sent:
                self.headers_for(502, "application/json")
                self.wfile.write(b'{"detail":"Review API unavailable"}')
        finally:
            connection.close()


if __name__ == "__main__":
    if UPSTREAM.scheme not in {"http", "https"} or not UPSTREAM.hostname or UPSTREAM.username or UPSTREAM.query or UPSTREAM.fragment:
        raise ValueError("REVIEWER_API_URL must be an HTTP(S) origin with optional base path")
    ThreadingHTTPServer((os.environ.get("FRONTEND_HOST", "127.0.0.1"), int(os.environ.get("PORT", "8080"))), Handler).serve_forever()
