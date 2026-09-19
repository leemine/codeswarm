"""Synthetic port-0 HTTP service for testctl infrastructure tests only."""

from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")


server = HTTPServer(("127.0.0.1", 0), Handler)
print(f"TESTCTL_PORT={server.server_port}", flush=True)
server.serve_forever()
