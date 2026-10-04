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

from crisol import deploy, manager, paths  # noqa: E402
from crisol.games import Game  # noqa: E402
from crisol.store import ModRecord  # noqa: E402

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
        # Steam «verifica» y repone el original: Crisol no debe borrarlo al restaurar.
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


class NoExtensionTest(Base):
    game_files = {"Game.exe": b"exe"}

    def test_zip_without_extension_is_extracted(self):
        z = make_zip(self.tmp / "dl" / "0b_53_a2_0b53a286", {"Mod/BepInEx/x.dll": b"x", "Mod/y.dll": b"y"})
        rec = manager.install_archive(self.ctx, z, ModRecord(uid=self.ctx.state.new_uid(), name="M"))
        self.assertEqual(len(rec.files), 2)


class ME3Test(Base):
    game_files = {"Game/eldenring.exe": b"exe", "Game/regulation.bin": b"r"}

    def test_profile_from_mod_and_plain_package(self):
        prof = (b'profileVersion = "v1"\nsavefile = "ER0000.cnv"\n[[supports]]\ngame = "eldenring"\n'
                b'[[package]]\nid = "c"\npath = "./../mod"\n[[natives]]\npath = "./../mod/dll/a.dll"\n')
        conv = self.add("Convergence", {"ConvergenceER/me3/convergence.me3": prof,
                                        "ConvergenceER/me3/convergence - seamless.me3": b"x",
                                        "ConvergenceER/me3/Linux/me3": b"#!/bin/sh\n",
                                        "ConvergenceER/me3/Linux/win64/me3.exe": b"x",
                                        "ConvergenceER/mod/regulation.bin": b"c", "ConvergenceER/mod/dll/a.dll": b"d"})
        other = self.add("Armor", {"Armor/mod/parts/x.dcx": b"p", "Armor/mod/regulation.bin": b"o"})
        self.assertEqual(self.ctx.layout.id, "me3")
        self.assertEqual((conv.packages, conv.natives, conv.savefile),
                         (["ConvergenceER/mod"], ["ConvergenceER/mod/dll/a.dll"], "ER0000.cnv"))
        self.assertEqual(other.packages, ["Armor/mod"])
        plan = self.ctx.plan()
        self.assertEqual([(c.target, c.winner) for c in plan.conflicts], [("regulation.bin", other.uid)])
        manager.apply(self.ctx)
        text = self.ctx.me3_profile.read_text()
        self.assertIn('savefile = "ER0000.cnv"', text)
        self.assertLess(text.index("ConvergenceER/mod"), text.index("Armor/mod"))
        cmd = manager.me3_command(self.ctx)
        self.assertTrue(cmd[0].endswith("me3/Linux/me3") and cmd[1:4] == ["launch", "--game", "eldenring"])
        self.assertTrue(self.ctx.loader().installed)
        # El juego no se toca: nada añadido ni cambiado.
        self.assertEqual(deploy.diff(self.before, deploy.fingerprint(self.game_dir)),
                         {"added": [], "removed": [], "changed": []})
        manager.restore(self.ctx)
        self.assertFalse(self.ctx.me3_profile.exists())

    def test_variants_and_options(self):
        base = b'profileVersion = "v1"\n[[supports]]\ngame = "eldenring"\n[[package]]\nid = "c"\npath = "./../mod"\n'
        seam = base + b'[[natives]]\npath = "./../SeamlessCoop/ersc.dll"\n'
        files = {"C/me3/convergence.me3": base, "C/me3/convergence - seamless.me3": seam,
                 "C/me3/Linux/me3": b"#!/bin/sh\n", "C/me3/Linux/win64/me3.exe": b"x", "C/mod/regulation.bin": b"c"}
        rec = self.add("Conv", files)
        self.assertEqual(rec.me3_variants, ["convergence"])  # seamless no: falta SeamlessCoop/ersc.dll
        files["C/SeamlessCoop/ersc.dll"] = b"dll"
        rec = self.add("Conv2", files)
        self.assertEqual(rec.me3_variants, ["convergence", "convergence - seamless"])
        rec.me3_variant = "convergence - seamless"
        manager.remap(self.ctx)
        self.assertEqual(rec.natives, ["C/SeamlessCoop/ersc.dll"])
        self.ctx.state.me3_opts = {"skip_logos": True}
        self.assertEqual(manager.me3_command(self.ctx)[-2:], ["--show-logos", "false"])


class FomodInstallTest(Base):
    game_files = {"Game.exe": b"exe", "Game_Data/x": b"x"}

    def test_install_with_fomod(self):
        from tests.test_fomod import XML
        files = {"Mod/fomod/ModuleConfig.xml": XML.replace('encoding="UTF-16"', 'encoding="UTF-8"').encode(),
                 "Mod/Core/base.txt": b"base", "Mod/Tex/2k/armor.dds": b"2k", "Mod/Tex/4k/armor.dds": b"4k",
                 "Mod/Tex/4k/parallax.dds": b"px", "Mod/Extras/Cape/cape.dds": b"c", "Mod/Patch/4k.ini": b"i"}
        seen = {}

        def chooser(module, root, previous):
            seen["previous"] = previous
            return {(0, 0): {0}, (0, 1): set()}  # 2K, sin extras

        z = make_zip(self.tmp / "dl/fomod.zip", files)
        rec = manager.install_archive(self.ctx, z, ModRecord(uid=self.ctx.state.new_uid(), name="Armor"),
                                      chooser=chooser)
        self.assertEqual(rec.fomod_name, "Better Armor")
        self.assertIsNone(seen["previous"])
        self.assertEqual(sorted(d for _, d in rec.files), ["base.txt", "textures/armor.dds"])
        # Reinstalar sin selector reutiliza la elección guardada (2K).
        rec = manager.install_archive(self.ctx, z, rec)
        self.assertEqual(sorted(d for _, d in rec.files), ["base.txt", "textures/armor.dds"])
        manager.apply(self.ctx)
        self.assertEqual((self.game_dir / "textures/armor.dds").read_bytes(), b"2k")
        self.assert_restored()
        # Cancelar el asistente no deja nada a medias.
        with self.assertRaises(manager.InstallCancelled):
            manager.install_archive(self.ctx, z, ModRecord(uid=self.ctx.state.new_uid(), name="B"),
                                    chooser=lambda *a: None)
        self.assertEqual(len(self.ctx.state.mods), 1)


class RunningTest(Base):
    game_files = {"Game.exe": b"exe"}

    def _spawn(self, *args, cwd=None):
        import subprocess
        import sys
        import time
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", *args], cwd=cwd)
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        time.sleep(0.2)
        return p

    def test_detects_steam_and_proton_processes(self):
        from crisol import running
        self.assertEqual(running.running(self.game), [])
        # Steam: «reaper SteamLaunch AppId=<id>» mientras el juego está abierto (el juego falso es AppId 1).
        steam = self._spawn("SteamLaunch", "AppId=1")
        self.assertTrue(running.running(self.game))
        with self.assertRaises(running.GameRunning):
            manager.apply(self.ctx)
        steam.kill()
        steam.wait()
        self.assertEqual(running.running(self.game), [])
        # Proton: un .exe con la carpeta del juego como directorio de trabajo.
        self._spawn("Z:" + str(self.game_dir).replace("/", "\\") + "\\Game.exe", cwd=self.game_dir)
        self.assertEqual(running.running(self.game), ["Game.exe"])
        # Un proceso cualquiera (p. ej. un terminal) en la carpeta del juego no cuenta.
        other = self._spawn("nada", cwd=self.game_dir)
        self.assertEqual(running.running(self.game), ["Game.exe"])
        other.kill()


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
