"""Pruebas sin red: enlaces nxm, emparejado de juegos y detección del tipo de juego."""
import tempfile
import unittest
from pathlib import Path

from forja.layouts import KCD2Layout, LooseLayout, UnrealLayout, detect
from forja.providers.base import ProviderError
from forja.providers.nexus import Nexus, parse_nxm

GAMES = [
    {"id": 1, "name": "Lords of the Fallen", "domain_name": "lordsofthefallen"},
    {"id": 2, "name": "Lords of the Fallen (2023)", "domain_name": "lordsofthefallen2023"},
    {"id": 3, "name": "World of Warcraft", "domain_name": "worldofwarcraft"},
    {"id": 4, "name": "Kingdom Come: Deliverance II", "domain_name": "kingdomcomedeliverance2"},
]


class FakeNexus(Nexus):
    def games(self):
        return GAMES


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
