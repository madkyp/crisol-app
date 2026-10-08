"""Colecciones completas: manifiesto, orden con reglas y archivos incluidos."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from crisol import collection
from crisol.providers.base import ProviderError

DATA = {
    "info": {"name": "Prueba"},
    "mods": [
        {"name": "Ajustes", "version": "1", "source": {"type": "bundle", "fileExpression": "ajustes"}},
        {"name": "Base", "version": "2.0", "source": {"type": "nexus", "modId": 10, "fileId": 100,
                                                       "md5": "AAA", "logicalFilename": "Base Mod"},
         "choices": {"type": "fomod", "options": [{"name": "Paso", "groups": []}]}},
        {"name": "Parche", "optional": True, "source": {"type": "nexus", "modId": 11, "fileId": 110,
                                                         "logicalFilename": "Parche"}},
        {"name": "Fuera", "source": {"type": "browse", "url": "https://ejemplo.org/x"},
         "instructions": "Bájalo de su web"},
    ],
    "modRules": [
        {"type": "after", "source": {"fileExpression": "ajustes"}, "reference": {"fileMD5": "aaa"}},
        {"type": "before", "source": {"logicalFileName": "Base Mod"}, "reference": {"logicalFileName": "Parche"}},
        {"type": "after", "source": {"fileMD5": "nada"}, "reference": {"fileMD5": "aaa"}},   # sin destino: se ignora
    ],
}


class CollectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_parse_and_order(self):
        man = collection.parse_data(DATA, self.tmp / "x.7z")
        self.assertEqual(man.name, "Prueba")
        kinds = {i.name: i.kind for i in man.items}
        self.assertEqual(kinds["Fuera"], "browse")
        base = next(i for i in man.items if i.name == "Base")
        self.assertEqual((base.mod_id, base.file_id, base.md5), (10, 100, "aaa"))
        self.assertEqual(base.choices, [{"name": "Paso", "groups": []}])
        order = [i.name for i in collection.ordered(man)]
        self.assertLess(order.index("Base"), order.index("Ajustes"))   # «Ajustes» después de «Base»
        self.assertLess(order.index("Base"), order.index("Parche"))
        self.assertEqual(sorted(order), sorted(kinds))

    def test_cycle_does_not_hang(self):
        data = dict(DATA, modRules=[
            {"type": "after", "source": {"fileMD5": "aaa"}, "reference": {"logicalFileName": "Parche"}},
            {"type": "after", "source": {"logicalFileName": "Parche"}, "reference": {"fileMD5": "aaa"}}])
        self.assertEqual(len(collection.ordered(collection.parse_data(data, self.tmp / "x.7z"))), 4)

    @unittest.skipUnless(shutil.which("bsdtar"), "hace falta bsdtar")
    def test_archive_and_bundled(self):
        src = self.tmp / "src"
        (src / "bundled" / "ajustes").mkdir(parents=True)
        (src / "bundled" / "ajustes" / "a.ini").write_text("x=1")
        (src / "collection.json").write_text(json.dumps(DATA))
        arc = self.tmp / "c.zip"
        subprocess.run(["bsdtar", "-a", "-cf", str(arc), "-C", str(src), "collection.json", "bundled"], check=True)
        man = collection.parse(arc)
        item = next(i for i in man.items if i.kind == "bundle")
        out = collection.extract_bundled(man, item, self.tmp / "out")
        self.assertTrue((out / "a.ini").is_file())
        item.bundled = "no-existe"
        with self.assertRaises(ProviderError):
            collection.extract_bundled(man, item, self.tmp / "out2")


if __name__ == "__main__":
    unittest.main()
