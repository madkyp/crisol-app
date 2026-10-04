"""Pruebas del núcleo con juegos falsos: instalar, ordenar, conflictos, aplicar y restaurar."""
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

_TMP = tempfile.TemporaryDirectory()
os.environ["XDG_DATA_HOME"] = f"{_TMP.name}/data"
os.environ["XDG_CONFIG_HOME"] = f"{_TMP.name}/config"
os.environ["XDG_CACHE_HOME"] = f"{_TMP.name}/cache"

from forja import deploy, manager, paths  # noqa: E402
from forja.games import Game  # noqa: E402
from forja.store import ModRecord  # noqa: E402

paths.ensure_dirs()


def write(root: Path, files: dict[str, bytes]) -> None:
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def make_zip(path: Path, files: dict[str, bytes]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for rel, data in files.items():
            z.writestr(rel, data)
    return path


def pak(files: dict[str, bytes]) -> bytes:
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for rel, data in files.items():
            z.writestr(rel, data)
    return buf.getvalue()


class Base(unittest.TestCase):
    game_files: dict[str, bytes] = {}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(dir=_TMP.name))
        self.game_dir = self.tmp / "game"
        write(self.game_dir, self.game_files)
        self.game = Game(key=f"steam:{self.id()}", source="steam", source_id="1", name="Test",
                         install_dir=self.game_dir)
        manager._contexts.clear()
        self.ctx = manager.context(self.game)
        self.before = deploy.fingerprint(self.game_dir)

    def add(self, name: str, files: dict[str, bytes], layout: str | None = None) -> ModRecord:
        z = make_zip(self.tmp / "dl" / f"{name}.zip", files)
        rec = ModRecord(uid=self.ctx.state.new_uid(), name=name)
        return manager.install_archive(self.ctx, z, rec, layout)

    def assert_restored(self):
        manager.restore(self.ctx)
        self.assertEqual(deploy.diff(self.before, deploy.fingerprint(self.game_dir)),
                         {"added": [], "removed": [], "changed": []})


class LooseTest(Base):
    game_files = {"Game.exe": b"exe", "Game_Data/level0": b"original", "UnityPlayer.dll": b"u"}

    def test_anchor_conflict_restore(self):
        # Envoltorio + BepInEx: debe colocarse sobre la raíz del juego.
        a = self.add("A", {"A v1/BepInEx/plugins/a.dll": b"a", "A v1/winhttp.dll": b"w", "A v1/readme.txt": b"r",
                           "A v1/Game_Data/level0": b"A"})
        b = self.add("B", {"Game_Data/level0": b"B", "Game_Data/new": b"n"})
        self.assertIn(["A v1/BepInEx/plugins/a.dll", "BepInEx/plugins/a.dll"], a.files)
        self.assertIn("A v1/readme.txt", a.skipped)
        plan = self.ctx.plan()
        c = {x.target: x for x in plan.conflicts}
        self.assertEqual(c["Game_Data/level0"].winner, b.uid)        # B va después: gana
        self.assertTrue(any("WINEDLLOVERRIDES" in n for n in plan.notes))
        manager.apply(self.ctx)
        self.assertEqual((self.game_dir / "Game_Data/level0").read_bytes(), b"B")
        # Reordenar: A al final → gana A, sin perder nada.
        self.ctx.state.move(a.uid, 1)
        manager.apply(self.ctx)
        self.assertEqual((self.game_dir / "Game_Data/level0").read_bytes(), b"A")
        # Desactivar A → vuelve B.
        self.ctx.state.set_enabled(a.uid, False)
        manager.apply(self.ctx)
        self.assertEqual((self.game_dir / "Game_Data/level0").read_bytes(), b"B")
        self.assertFalse((self.game_dir / "BepInEx").exists())
        self.assert_restored()
        self.assertEqual((self.game_dir / "Game_Data/level0").read_bytes(), b"original")
        self.assertFalse(self.ctx.deployer.backup_root.exists())

    def test_case_insensitive_and_changed_by_other(self):
        write(self.game_dir, {"BepInEx/config/x.cfg": b"cfg"})
        self.before = deploy.fingerprint(self.game_dir)
        self.add("A", {"bepinex/plugins/a.dll": b"a", "UnityPlayer.dll": b"modded"})
        manager.apply(self.ctx)
        self.assertTrue((self.game_dir / "BepInEx/plugins/a.dll").exists())
        self.assertFalse((self.game_dir / "bepinex").exists())
        # Steam «verifica» y repone el original: Forja no debe borrarlo al restaurar.
        (self.game_dir / "UnityPlayer.dll").unlink()
        (self.game_dir / "UnityPlayer.dll").write_bytes(b"u")
        r = manager.restore(self.ctx)
        self.assertEqual(r.kept_changed, ["UnityPlayer.dll"])
        self.assertEqual((self.game_dir / "UnityPlayer.dll").read_bytes(), b"u")
        self.assertFalse((self.game_dir / "BepInEx/plugins").exists())
        self.assertTrue((self.game_dir / "BepInEx/config/x.cfg").exists())

    def test_profiles_and_update_keeps_position(self):
        a = self.add("A", {"a.txt.dll": b"1"})
        b = self.add("B", {"b.dll": b"1"})
        st = self.ctx.state
        st.add_profile("Sin mods")
        st.switch("Sin mods")
        self.assertEqual(st.enabled_ordered(), [])
        st.switch("Predeterminado")
        st.move(a.uid, 1)
        # Actualizar A (mismo uid) no cambia su posición.
        z = make_zip(self.tmp / "dl/A2.zip", {"a.txt.dll": b"2"})
        manager.install_archive(self.ctx, z, a)
        self.assertEqual(st.profile.order, [b.uid, a.uid])
        manager.apply(self.ctx)
        self.assertEqual((self.game_dir / "a.txt.dll").read_bytes(), b"2")
        self.assert_restored()


class KCD2Test(Base):
    game_files = {"Bin/Win64MasterMasterSteamPGO/KingdomCome.exe": b"exe", "Data/IPL_GameData.pak": b"x"}

    def test_folders_order_and_internal_conflicts(self):
        p1 = pak({"Libs/Tables/item.xml": b"1", "a.lua": b"a"})
        p2 = pak({"libs/tables/item.xml": b"2"})
        a = self.add("Cheat", {"Cheat/mod.manifest": b"<m/>", "Cheat/Data/cheat.pak": p1})
        b = self.add("Better Loot", {"Mods/loot/mod.manifest": b"<m/>", "Mods/loot/Data/loot.pak": p2})
        c = self.add("NoManifest", {"Data/x.pak": pak({"z": b"z"})})
        self.assertEqual(a.folders, ["Cheat"])
        self.assertEqual(b.folders, ["loot"])
        self.assertEqual(c.folders, ["nomanifest"])
        write(self.game_dir, {"mods/handmade/mod.manifest": b"<m/>"})
        self.before = deploy.fingerprint(self.game_dir)
        plan = self.ctx.plan(with_internal=True)
        internal = [x for x in plan.conflicts if x.internal]
        self.assertEqual([(x.target, x.winner) for x in internal], [("libs/tables/item.xml", b.uid)])
        manager.apply(self.ctx)
        order = (self.game_dir / "mods/mod_order.txt").read_text().splitlines()
        names = [line for line in order if not line.startswith("#")]
        self.assertEqual(names, ["Cheat", "loot", "nomanifest", "handmade"])
        self.ctx.state.set_enabled(a.uid, False)
        manager.apply(self.ctx)
        self.assertFalse((self.game_dir / "mods/Cheat").exists())
        self.assert_restored()
        self.assertTrue((self.game_dir / "mods/handmade/mod.manifest").exists())


class UnrealTest(Base):
    game_files = {"LiesofP/Content/Paks/pakchunk0-WindowsNoEditor.pak": b"p",
                  "LiesofP/Binaries/Win64/LOP-Win64-Shipping.exe": b"e", "LOP.exe": b"l", "Engine/x": b"x"}

    def test_paks_prefixed_by_order(self):
        a = self.add("A", {"A/zzz_A_P.pak": b"a", "A/zzz_A_P.ucas": b"a", "A/zzz_A_P.utoc": b"a"})
        b = self.add("B", {"LiesofP/Binaries/Win64/dwmapi.dll": b"d", "LiesofP/Binaries/Win64/ue4ss/x.lua": b"l"})
        self.assertIn(["A/zzz_A_P.pak", "LiesofP/Content/Paks/~mods/zzz_A_P.pak"], a.files)
        self.assertIn(["LiesofP/Binaries/Win64/dwmapi.dll", "LiesofP/Binaries/Win64/dwmapi.dll"], b.files)
        manager.apply(self.ctx)
        mods = sorted(p.name for p in (self.game_dir / "LiesofP/Content/Paks/~mods").iterdir())
        self.assertEqual(mods, ["001_zzz_A_P.pak", "001_zzz_A_P.ucas", "001_zzz_A_P.utoc"])
        self.ctx.state.move(a.uid, 1)
        manager.apply(self.ctx)
        mods = sorted(p.name for p in (self.game_dir / "LiesofP/Content/Paks/~mods").iterdir())
        self.assertEqual(mods[0], "002_zzz_A_P.pak")
        self.assert_restored()
        self.assertFalse((self.game_dir / "LiesofP/Content/Paks/~mods").exists())


if __name__ == "__main__":
    unittest.main()
