"""Real loopback handshake regressions; no model or external network access."""
import threading
import time
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from jiuwenswarm.channels.web.app_web import _SpaStaticHandler


@pytest.mark.parametrize("delay", [0, 0.6])
def test_proxy_handshake_has_its_own_timeout_after_tcp_connect(delay):
    class Upstream(BaseHTTPRequestHandler):
        def do_GET(self):
            time.sleep(delay)
            try:
                self.send_response(101)
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.end_headers()
            except OSError:
                pass

        def log_message(self, *args):
            pass

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)

    class Proxy(_SpaStaticHandler):
        ws_target = f"ws://127.0.0.1:{upstream.server_port}"
        _WS_CONNECT_TIMEOUT = 2

        def log_message(self, *args):
            pass

    proxy = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    servers = [upstream, proxy]
    threads = [threading.Thread(target=s.serve_forever) for s in servers]
    for thread in threads:
        thread.start()
    connection = HTTPConnection("127.0.0.1", proxy.server_port, timeout=3)
    try:
        connection.request("GET", "/ws", headers={"Upgrade": "websocket", "Connection": "Upgrade"})
        response = connection.getresponse()
        try:
            assert response.status == 101
        finally:
            response.close()
    finally:
        connection.close()
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join()
