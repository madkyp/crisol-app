"""Pruebas sin red: enlaces nxm, emparejado de juegos y detección del tipo de juego."""
import tempfile
import unittest
from pathlib import Path

from crisol.layouts import KCD2Layout, LooseLayout, UnrealLayout, detect
from crisol.providers.base import ProviderError
from crisol.providers.nexus import Nexus, parse_nxm

GAMES = [
    {"id": 1, "name": "Lords of the Fallen", "domain_name": "lordsofthefallen"},
    {"id": 2, "name": "Lords of the Fallen (2023)", "domain_name": "lordsofthefallen2023"},
    {"id": 3, "name": "World of Warcraft", "domain_name": "worldofwarcraft"},
    {"id": 4, "name": "Kingdom Come: Deliverance II", "domain_name": "kingdomcomedeliverance2"},
]


class FakeNexus(Nexus):
    def games(self):
        return GAMES


class EndorseTest(unittest.TestCase):
    def _resp(self, code, body):
        from unittest import mock
        r = mock.Mock(status_code=code, headers={})
        r.json.return_value = body
        r.text = str(body)
        return r

    def test_endorse_post_and_errors(self):
        from unittest import mock
        n = Nexus("k")
        n.session = mock.Mock()
        n.session.post.return_value = self._resp(200, {"message": "ok", "status": "Endorsed"})
        self.assertEqual(n.endorse("eldenring", 510, "2.0.1"), "Endorsed")
        url = n.session.post.call_args[0][0]
        self.assertTrue(url.endswith("/games/eldenring/mods/510/endorse.json"))
        self.assertEqual(n.session.post.call_args[1]["json"], {"Version": "2.0.1"})
        n.session.post.return_value = self._resp(200, {"status": "Abstained"})
        self.assertEqual(n.endorse("eldenring", 510, "2.0.1", on=False), "Abstained")
        self.assertTrue(n.session.post.call_args[0][0].endswith("/abstain.json"))
        n.session.post.return_value = self._resp(403, {"message": "NOT_DOWNLOADED_MOD"})
        with self.assertRaisesRegex(ProviderError, "descargado"):
            n.endorse("eldenring", 510, "2.0.1")
        n.session.get.return_value = self._resp(200, {"name": "x", "endorsement": {"endorse_status": "Endorsed"}})
        self.assertEqual(n.endorsements("eldenring", [510]), {("eldenring", 510): "Endorsed"})


class NexusTest(unittest.TestCase):
    def test_parse_nxm(self):
        link = parse_nxm("nxm://LiesOfP/mods/12/files/345?key=abc&expires=1700000000&user_id=9")
        self.assertEqual((link.domain, link.mod_id, link.file_id, link.key, link.expires),
                         ("liesofp", 12, 345, "abc", 1700000000))
        with self.assertRaises(ProviderError):
            parse_nxm("nxm://liesofp/collections/abc/revisions/3")

    def test_find_game(self):
        n = FakeNexus()
        self.assertEqual(n.find_game("Kingdom Come: Deliverance II")[0], "kingdomcomedeliverance2")
        # Mismo nombre en Steam, juego distinto en Nexus: manda la tabla por appid.
        self.assertEqual(n.find_game("Lords of the Fallen", "1501750")[0], "lordsofthefallen2023")
        self.assertEqual(n.find_game("World of Warcraft: Forever (beta)")[0], "worldofwarcraft")
        self.assertIsNone(n.find_game("Piramide - El Sueño del Faraon"))

    def test_detect_layout(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            self.assertIs(detect(d, "kingdomcomedeliverance2"), KCD2Layout)
            (d / "Game" / "Content" / "Paks").mkdir(parents=True)
            self.assertIs(detect(d, "x"), UnrealLayout)
        with tempfile.TemporaryDirectory() as t:
            self.assertIs(detect(Path(t), "x"), LooseLayout)


if __name__ == "__main__":
    unittest.main()
