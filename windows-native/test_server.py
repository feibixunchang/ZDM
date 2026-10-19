"""Local file server used by Zdm.exe --selftest.

Serves a generated payload with Range support and a little throttling so the
progress curve is observable. Self-contained: only needs Python 3 stdlib.

    python test_server.py            # serves on 127.0.0.1:8899
    ZDM_TEST_URL=http://127.0.0.1:8899/payload.bin Zdm.exe --selftest
"""
import http.server
import os
import socketserver
import threading
import time

SIZE = 24 * 1024 * 1024
PAYLOAD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "payload.bin")


def ensure_payload() -> str:
    if not os.path.exists(PAYLOAD) or os.path.getsize(PAYLOAD) != SIZE:
        with open(PAYLOAD, "wb") as f:
            f.write(os.urandom(SIZE))
    return PAYLOAD


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            s, _, e = rng[6:].partition("-")
            start = int(s)
            end = int(e) if e else SIZE - 1
            end = min(end, SIZE - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{SIZE}")
        else:
            start, end = 0, SIZE - 1
            self.send_response(200)

        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header(
            "Content-Disposition", 'attachment; filename="zdm-native-selftest.bin"')
        self.end_headers()

        with open(PAYLOAD, "rb") as f:
            f.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = f.read(min(512 * 1024, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except Exception:
                    return
                remaining -= len(chunk)
                # Throttle a little so several polls see intermediate progress.
                time.sleep(0.004)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    ensure_payload()
    with Server(("127.0.0.1", 8899), Handler) as httpd:
        print(f"serving {SIZE} bytes on http://127.0.0.1:8899/payload.bin", flush=True)
        httpd.serve_forever()


if __name__ == "__main__":
    main()