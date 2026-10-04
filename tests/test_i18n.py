import ast
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "crisol"


def msgids():
    ids = set()
    for f in ROOT.rglob("*.py"):
        for n in ast.walk(ast.parse(f.read_text())):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_" and n.args \
                    and isinstance(n.args[0], ast.Constant):
                ids.add(n.args[0].value)
    return ids


class TestTranslations(unittest.TestCase):
    def test_every_text_has_english(self):
        en = json.loads((ROOT / "locale" / "en.json").read_text())
        missing = sorted(msgids() - set(en))
        self.assertEqual(missing, [], "Textos sin traducir al inglés")

    def test_placeholders_match(self):
        en = json.loads((ROOT / "locale" / "en.json").read_text())
        ph = re.compile(r"\{\d+[^}]*\}")
        bad = [k for k, v in en.items() if sorted(ph.findall(k)) != sorted(ph.findall(v))]
        self.assertEqual(bad, [])

    def test_language_switch(self):
        from crisol import i18n
        old = i18n.LANG
        try:
            i18n.LANG, i18n._EN = "en", json.loads((ROOT / "locale" / "en.json").read_text())
            self.assertEqual(i18n._("Jugar con mods"), "Play with mods")
            self.assertEqual(i18n._("texto sin traducción"), "texto sin traducción")   # nunca rompe
            i18n.LANG = "es"
            self.assertEqual(i18n._("Jugar con mods"), "Jugar con mods")
        finally:
            i18n.LANG = old


if __name__ == "__main__":
    unittest.main()
