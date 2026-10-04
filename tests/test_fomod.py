"""FOMOD: lectura, pasos condicionados, opciones por defecto, validación y construcción."""
import tempfile
import unittest
from pathlib import Path

from crisol import fomod

XML = """<?xml version="1.0" encoding="UTF-16"?>
<config xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
        xsi:noNamespaceSchemaLocation="http://qconsulting.ca/fo3/ModConfig5.0.xsd">
  <moduleName>Better Armor</moduleName>
  <requiredInstallFiles><folder source="Core" destination="" /></requiredInstallFiles>
  <installSteps order="Explicit">
    <installStep name="Estilo">
      <optionalFileGroups order="Explicit">
        <group name="Textura" type="SelectExactlyOne">
          <plugins order="Explicit">
            <plugin name="2K"><description>Normal</description>
              <files><file source="Tex\\2k\\armor.dds" destination="textures\\armor.dds" /></files>
              <conditionFlags><flag name="res">2k</flag></conditionFlags>
              <typeDescriptor><type name="Optional"/></typeDescriptor></plugin>
            <plugin name="4K"><description>Alta</description>
              <files><file source="Tex\\4k\\armor.dds" destination="textures\\armor.dds" /></files>
              <conditionFlags><flag name="res">4k</flag></conditionFlags>
              <typeDescriptor><type name="Recommended"/></typeDescriptor></plugin>
          </plugins>
        </group>
        <group name="Extras" type="SelectAny">
          <plugins order="Explicit">
            <plugin name="Capa"><description/><files><folder source="Extras/Cape" destination="cape" priority="1"/></files>
              <typeDescriptor><type name="Optional"/></typeDescriptor></plugin>
            <plugin name="Roto"><description/><files/>
              <typeDescriptor><type name="NotUsable"/></typeDescriptor></plugin>
          </plugins>
        </group>
      </optionalFileGroups>
    </installStep>
    <installStep name="Solo 4K">
      <visible><flagDependency flag="res" value="4k"/></visible>
      <optionalFileGroups order="Explicit">
        <group name="Parallax" type="SelectAtMostOne">
          <plugins order="Explicit">
            <plugin name="Sí"><description/><files><file source="Tex/4k/parallax.dds" destination="textures/parallax.dds"/></files>
              <typeDescriptor><type name="Optional"/></typeDescriptor></plugin>
          </plugins>
        </group>
      </optionalFileGroups>
    </installStep>
  </installSteps>
  <conditionalFileInstalls><patterns>
    <pattern><dependencies operator="And"><flagDependency flag="res" value="4k"/></dependencies>
      <files><file source="Patch/4k.ini" destination="config/res.ini"/></files></pattern>
  </patterns></conditionalFileInstalls>
</config>
"""


class FomodTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        files = {"Core/base.txt": "base", "Tex/2k/armor.dds": "2k", "Tex/4k/armor.dds": "4k",
                 "Tex/4k/parallax.dds": "px", "Extras/Cape/cape.dds": "cape", "Patch/4k.ini": "ini"}
        for rel, text in files.items():
            p = self.root / "BetterArmor" / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        cfg = self.root / "BetterArmor" / "fomod" / "ModuleConfig.xml"
        cfg.parent.mkdir()
        cfg.write_bytes(b"\xff\xfe" + XML.encode("utf-16-le"))
        self.mod_root = self.root / "BetterArmor"

    def test_full_flow(self):
        cfg = fomod.find_config(self.root)
        self.assertIsNotNone(cfg)
        m = fomod.parse(cfg)
        self.assertEqual(m.name, "Better Armor")
        self.assertEqual([s.name for s in m.steps], ["Estilo", "Solo 4K"])
        # Por defecto: la recomendada (4K); el paso «Solo 4K» se ve; lo no disponible no se elige.
        choice = fomod.default_choice(m)
        self.assertEqual(choice[(0, 0)], {1})
        self.assertEqual(choice[(0, 1)], set())
        self.assertEqual(fomod.visible_steps(m, choice), [0, 1])
        out = self.root / "out"
        fomod.build(m, choice, self.mod_root, out)
        self.assertEqual((out / "textures/armor.dds").read_text(), "4k")
        self.assertEqual((out / "config/res.ini").read_text(), "ini")   # condicional por bandera
        self.assertTrue((out / "base.txt").exists())                    # obligatorio, carpeta a la raíz
        # Con 2K: el paso «Solo 4K» desaparece y el condicional no se instala.
        choice[(0, 0)] = {0}
        choice[(0, 1)] = {0}
        choice[(1, 0)] = {0}
        self.assertEqual(fomod.visible_steps(m, choice), [0])
        out2 = self.root / "out2"
        fomod.build(m, choice, self.mod_root, out2)
        self.assertEqual((out2 / "textures/armor.dds").read_text(), "2k")
        self.assertFalse((out2 / "config/res.ini").exists())
        self.assertFalse((out2 / "textures/parallax.dds").exists())   # su paso no era visible
        self.assertTrue((out2 / "cape/cape.dds").exists())
        # Validación de grupos y guardado de la elección.
        self.assertIsNotNone(fomod.validate_group(m.steps[0].groups[0], set()))
        self.assertIsNone(fomod.validate_group(m.steps[0].groups[0], {1}))
        self.assertEqual(fomod.choice_from_json(fomod.choice_to_json(choice)), choice)


if __name__ == "__main__":
    unittest.main()
