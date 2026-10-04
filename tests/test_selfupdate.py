"""Aviso de versión nueva: compara versiones y respeta la caché diaria (sin red)."""
import os
import tempfile
import time
import unittest

os.environ["XDG_CACHE_HOME"] = tempfile.mkdtemp()

from crisol import VERSION, jsonio, paths, selfupdate  # noqa: E402


class SelfUpdateTest(unittest.TestCase):
    def cached(self, version):
        jsonio.save(paths.CACHE_DIR / "selfupdate.json",
                    {"checked": time.time(), "version": version, "url": "u", "asset": "a.pkg.tar.zst"})

    def test_newer_older_same(self):
        major, minor, patch = selfupdate._vtuple(VERSION)
        self.cached(f"{major}.{minor + 1}.0")
        self.assertEqual(selfupdate.latest()["asset"], "a.pkg.tar.zst")
        self.cached(VERSION)
        self.assertIsNone(selfupdate.latest())
        self.cached("0.0.1")
        self.assertIsNone(selfupdate.latest())
        self.assertGreater(selfupdate._vtuple("0.10.0"), selfupdate._vtuple("0.9.9"))


if __name__ == "__main__":
    unittest.main()
