"""Descargas: reanudar un .part con Range y empezar de nuevo si el servidor no lo admite."""
import hashlib
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault("XDG_DATA_HOME", tempfile.mkdtemp())

from crisol import manager  # noqa: E402

DATA = os.urandom(3 * 1024 * 1024 + 123)


def make_handler(ranges: bool):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            start = 0
            rng = self.headers.get("Range")
            if ranges and rng and rng.startswith("bytes="):
                start = int(rng[6:].split("-")[0])
                self.send_response(206)
            else:
                self.send_response(200)
            body = DATA[start:]
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    return H


class DownloadTest(unittest.TestCase):
    def serve(self, ranges: bool) -> str:
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ranges))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_address[1]}/mod.zip"

    def run_case(self, ranges: bool) -> list[str]:
        url = self.serve(ranges)
        dest = Path(tempfile.mkdtemp()) / "mod.zip"
        cancel = threading.Event()
        msgs: list[str] = []

        def stop_halfway(frac, msg):
            if frac > 0.4:
                cancel.set()
        with self.assertRaises(manager.DownloadError):
            manager.download(url, dest, len(DATA), stop_halfway, cancel)
        part = dest.with_suffix(".zip.part")
        self.assertTrue(0 < part.stat().st_size < len(DATA))  # el .part se conserva
        manager.download(url, dest, len(DATA), lambda f, m: msgs.append(m))
        self.assertEqual(hashlib.md5(dest.read_bytes()).hexdigest(), hashlib.md5(DATA).hexdigest())
        self.assertFalse(part.exists())
        return msgs

    def test_resume_with_range(self):
        msgs = self.run_case(ranges=True)
        self.assertIn("(reanudada)", msgs[-1])

    def test_restart_without_range(self):
        msgs = self.run_case(ranges=False)
        self.assertNotIn("(reanudada)", msgs[-1])


if __name__ == "__main__":
    unittest.main()
