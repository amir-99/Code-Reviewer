"""Offline proxy behavior tests: python3 -m unittest discover -s frontend."""

import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import server


class ProxyTests(unittest.TestCase):
    def handler(self, path="/api/admin/reviews/id/events"):
        handler = object.__new__(server.Handler)
        handler.path = path
        handler.command = "GET"
        handler.headers = {"Authorization": "Bearer private", "Last-Event-ID": "12"}
        handler.rfile = io.BytesIO()
        handler.wfile = io.BytesIO()
        handler.connection = SimpleNamespace(settimeout=lambda _: None)
        handler.sent = []
        handler.headers_for = lambda *args: handler.sent.append(args)
        handler.send_error = lambda code: handler.sent.append((code,))
        return handler

    def test_stream_forwards_header_and_flushes_without_buffering(self):
        handler = self.handler()
        chunks = iter([b": heartbeat\n\n", b"event: complete\ndata: {}\n\n", b""])
        response = SimpleNamespace(status=200, getheader=lambda *args: "text/event-stream", read1=lambda _: next(chunks))
        with patch.object(server.http.client, "HTTPConnection") as connection:
            connection.return_value.getresponse.return_value = response
            handler.do_GET()
            args = connection.return_value.request.call_args.args
            self.assertEqual(args[1], "/admin/reviews/id/events")
            self.assertEqual(args[3]["Authorization"], "Bearer private")
            self.assertEqual(args[3]["Last-Event-ID"], "12")
            connection.return_value.close.assert_called_once()
        self.assertIn(b"event: complete", handler.wfile.getvalue())
        self.assertEqual(handler.sent, [(200, "text/event-stream")])

    def test_no_static_traversal(self):
        handler = self.handler("/../server.py")
        handler.do_GET()
        self.assertEqual(handler.sent, [(404,)])

    def test_body_limit_and_no_upstream_error_leak(self):
        handler = self.handler()
        handler.headers["Content-Length"] = "999999"
        handler.do_POST()
        self.assertEqual(handler.sent, [(413,)])
        handler = self.handler()
        with patch.object(server.http.client, "HTTPConnection") as connection:
            connection.return_value.request.side_effect = OSError("private upstream information")
            handler.do_GET()
        self.assertEqual(handler.sent[0][0], 502)
        self.assertNotIn(b"private", handler.wfile.getvalue())


if __name__ == "__main__":
    unittest.main()
