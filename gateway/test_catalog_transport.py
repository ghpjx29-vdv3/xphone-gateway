import json
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from tunnel_runtime import catalog_server


@contextmanager
def serving(server):
    with server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join()


class CatalogTransportTest(unittest.TestCase):
    def test_resumes_from_partial_body_with_same_version(self):
        expected = bytes(range(256)) * 330
        requests = []

        class Upstream(BaseHTTPRequestHandler):
            def do_POST(self):
                query = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                offset = query["offset"]
                requests.append((offset, query["version"]))
                chunk = expected[offset:offset + 8192]
                self.send_response(206)
                self.send_header("Content-Length", str(len(chunk)))
                self.send_header("Content-Range", f"bytes {offset}-{offset + len(chunk) - 1}/{len(expected)}")
                self.send_header("X-XPhone-Object-Version", "fixture")
                self.end_headers()
                self.wfile.write(chunk[:1093] if offset in (0, 1093) else chunk)

            def log_message(self, *_args):
                pass

        secret = "x" * 43
        with serving(ThreadingHTTPServer(("127.0.0.1", 0), Upstream)) as upstream:
            with serving(catalog_server({"relayOrigin": upstream, "gatewayId": "test", "relaySecret": secret})) as catalog:
                query = urlencode({"key": "packs/棠/manifest.json"})
                request = Request(f"{catalog}/gateway/catalog-object?{query}", headers={"X-XPhone-Relay-Key": secret})
                with urlopen(request, timeout=5) as response:
                    self.assertEqual(response.read(), expected)
        self.assertEqual(requests[:3], [(0, None), (1093, "fixture"), (2186, "fixture")])
