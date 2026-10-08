"""Pruebas del núcleo con juegos falsos: instalar, ordenar, conflictos, aplicar y restaurar."""
import os
import shutil
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

    def test_chosen_winner_per_file(self):
        from crisol import modlist
        a = self.add("A", {"Game_Data/level0": b"A", "Game_Data/x": b"Ax"})
        b = self.add("B", {"Game_Data/level0": b"B", "Game_Data/x": b"Bx"})
        st = self.ctx.state
        st.set_override("Game_Data/LEVEL0", a.uid)            # A gana level0 aunque B va después
        plan = self.ctx.plan()
        c = {x.target: x for x in plan.conflicts}
        self.assertEqual((c["Game_Data/level0"].winner, c["Game_Data/level0"].chosen), (a.uid, True))
        self.assertEqual((c["Game_Data/x"].winner, c["Game_Data/x"].chosen), (b.uid, False))
        manager.apply(self.ctx)
        self.assertEqual((self.game_dir / "Game_Data/level0").read_bytes(), b"A")
        self.assertEqual((self.game_dir / "Game_Data/x").read_bytes(), b"Bx")
        st.move(a.uid, 1)                                       # reordenar no quita la elección
        manager.apply(self.ctx)
        self.assertEqual((self.game_dir / "Game_Data/level0").read_bytes(), b"A")
        self.assertEqual((self.game_dir / "Game_Data/x").read_bytes(), b"Ax")
        st.set_override("Game_Data/level0", b.uid)
        data = modlist.export(self.ctx)
        self.assertEqual(next(m for m in data["mods"] if m["name"] == "B")["wins"], ["game_data/level0"])
        name, _missing = modlist.finish(self.ctx, data)
        self.assertEqual(st.profiles[name].overrides, {"game_data/level0": b.uid})
        st.set_override("Game_Data/level0", None)
        self.assertEqual(self.ctx.plan().files["Game_Data/level0"][0], a.uid)   # según el orden: A va último
        st.set_override("Game_Data/level0", b.uid)
        st.remove(b.uid)                                        # desinstalar al ganador limpia la elección
        self.assertEqual(st.profile.overrides, {})
        self.assert_restored()

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


class VersionTest(unittest.TestCase):
    def test_update_only_when_newer(self):
        def upd(cur, new):
            return ModRecord(uid="x", name="m", version=cur, latest_version=new).update_available
        self.assertFalse(upd("2.0.1", "2.0.0"))     # la página del mod va por detrás del archivo
        self.assertTrue(upd("1.5.1", "2.0.0"))
        self.assertFalse(upd("v1.2", "1.2"))
        self.assertTrue(upd("beta", "gamma"))         # sin números: basta con que sea distinta


class DeckOverridesTest(unittest.TestCase):
    def test_hint_hidden_when_profile_has_it(self):
        import json
        from unittest import mock
        from crisol import loaders
        home = Path(tempfile.mkdtemp(dir=_TMP.name))
        f = home / ".local/share/gaming-deck/gaming/profiles.json"
        f.parent.mkdir(parents=True)
        f.write_text(json.dumps({"steam:5": {"env": {"WINEDLLOVERRIDES": "dwmapi,winhttp=n,b;d3d9=b"}}}))
        with mock.patch.object(Path, "home", return_value=home), \
                mock.patch("shutil.which", return_value="/usr/bin/gaming-deck"):
            self.assertEqual(loaders.deck_overrides("steam:5"), {"dwmapi", "winhttp"})
            self.assertEqual(loaders.dll_override_hint(["dwmapi.dll"], "steam:5"), "")
            self.assertIn("version=n,b", loaders.dll_override_hint(["version.dll"], "steam:5"))
            self.assertIn("dwmapi=n,b", loaders.dll_override_hint(["dwmapi.dll"], "steam:6"))


class ModListTest(Base):
    game_files = {"Game.exe": b"exe", "UnityPlayer.dll": b"u"}

    def test_export_plan_finish(self):
        from crisol import modlist
        st = self.ctx.state
        a = self.add("A", {"a.dll": b"a"})
        b = self.add("B", {"b.dll": b"b"})
        loose = self.add("Suelto", {"c.dll": b"c"})
        for rec, mid, fid in ((a, 1, 10), (b, 2, 20)):
            rec.provider, rec.game_domain, rec.mod_id, rec.file_id = "nexus", "g", mid, fid
        a.fomod_choice, a.fomod_name = [[0, 0, [1]]], "A"
        b.note = "con la opción X"
        st.nexus_domain = "g"
        st.move(b.uid, 0)
        st.set_enabled(loose.uid, False)
        data = modlist.export(self.ctx)
        self.assertEqual([m["name"] for m in data["mods"]], ["B", "A", "Suelto"])
        self.assertEqual(data["mods"][1]["fomod"]["choice"], [[0, 0, [1]]])
        self.assertFalse(data["mods"][2]["enabled"])
        # En «otro PC»: B tiene otra versión, A falta, «Suelto» está.
        st.remove(a.uid)
        b.file_id = 21
        plan = modlist.plan(self.ctx, data)
        self.assertEqual([e.status for e in plan.entries], ["other-version", "missing", "installed"])
        self.assertEqual(plan.entries[1].item.fomod_choice, [[0, 0, [1]]])
        before = st.active
        name, missing = modlist.finish(self.ctx, data)
        self.assertEqual(missing, ["A"])
        self.assertEqual(st.active, name)
        self.assertIn(before, st.profiles)                        # el perfil de antes sigue ahí
        self.assertEqual(st.profile.order, [b.uid, loose.uid])
        self.assertEqual(st.profile.enabled, [b.uid])
        self.assertEqual(data["mods"][0]["note"], "con la opción X")
        self.assertNotEqual(modlist.finish(self.ctx, data)[0], name)   # otro nombre si ya existe
        data["game"]["nexus"] = "otro"
        self.assertTrue(modlist.plan(self.ctx, data).other_game)


class BackupTest(Base):
    game_files = {"Game.exe": b"exe", "Game_Data/level0": b"orig"}

    def test_backup_same_pc_and_new_pc(self):
        import json
        from unittest import mock
        from crisol import backup, paths
        a = self.add("A", {"Game_Data/level0": b"A"})
        a.note = "mi nota"
        st = self.ctx.state
        st.save()
        manager.apply(self.ctx)
        saves = paths.DATA_DIR / "saves" / self.game.safe_key / "2026-01-01"
        saves.mkdir(parents=True)
        (saves / "ER0000.sl2").write_bytes(b"partida")
        arc = self.tmp / "copia.tar.gz"
        with mock.patch("crisol.games.scan_all", return_value=[self.game]):
            man = backup.create(arc, include_saves=True)
            self.assertIn(self.game.safe_key, [g["safe"] for g in man["games"]])
            self.assertEqual(backup.read_manifest(arc)["format"], "crisol-backup")
            # Mismo PC (los mods siguen): el estado vuelve tal cual, aunque se haya perdido.
            st.path.unlink()
            shutil.rmtree(saves)
            r = backup.restore(arc)
        self.assertIn(self.game.key, r.games)
        self.assertEqual(r.saves, 1)
        self.assertTrue(Path(r.safety).is_file())
        self.assertEqual(json.loads(st.path.read_text())["mods"][a.uid]["note"], "mi nota")
        self.assertEqual((saves / "ER0000.sl2").read_bytes(), b"partida")
        # PC nuevo (sin los mods): no se escribe el estado; queda la lista para importar.
        manager.restore(self.ctx)
        shutil.rmtree(st.staging(a.uid))
        st.path.unlink()
        with mock.patch("crisol.games.scan_all", return_value=[self.game]):
            r = backup.restore(arc)
        self.assertEqual((r.games, r.lists), ([], [self.game.key]))
        self.assertFalse(st.path.exists())
        self.assertEqual(backup.pending_list(self.game.safe_key)["mods"][0]["note"], "mi nota")
        backup.drop_pending(self.game.safe_key)
        self.assertIsNone(backup.pending_list(self.game.safe_key))
        bad = self.tmp / "otro.tar.gz"
        bad.write_bytes(b"no")
        with self.assertRaises(backup.BackupError):
            backup.read_manifest(bad)


class NotifyTest(unittest.TestCase):
    def test_once_disabled_and_timer(self):
        from unittest import mock
        from crisol import notify
        from crisol.config import Config
        notify.SEEN.unlink(missing_ok=True)
        with mock.patch("shutil.which", return_value="/usr/bin/notify-send"), \
                mock.patch("subprocess.run") as run:
            self.assertTrue(notify.send("t", "b", once=("build:x", "1")))
            self.assertFalse(notify.send("t", "b", once=("build:x", "1")))     # ya avisado
            self.assertTrue(notify.send("t", "b", once=("build:x", "2")))      # versión nueva: sí
            self.assertEqual(run.call_count, 2)
            self.assertIn("notify-send", run.call_args[0][0][0])
            cfg = Config.load()
            cfg.notifications = False
            cfg.save()
            self.assertFalse(notify.send("t", "b"))
            cfg.notifications = True
            cfg.save()
            notify.set_background_checks(True)
            self.assertTrue(notify.background_checks_on())
            self.assertIn("--check-updates --notify", (notify._unit_dir() / "crisol-updates.service").read_text())
            self.assertTrue(any("enable" in c[0][0] for c in run.call_args_list))
            notify.set_background_checks(False)
            self.assertFalse(notify.background_checks_on())


class ToggleCliTest(Base):
    game_files = {"Game.exe": b"exe"}

    def test_enable_disable_by_uid_and_name(self):
        import contextlib
        import io
        import json
        from crisol import app
        a = self.add("A", {"a.dll": b"a"})
        st = self.ctx.state
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(app.toggle_cli(self.ctx, a.uid, False), 0)
        self.assertFalse(st.is_enabled(a.uid))
        self.assertEqual(json.loads(out.getvalue())["enabled"], False)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(app.toggle_cli(self.ctx, "A", True), 0)
            self.assertEqual(app.toggle_cli(self.ctx, "nada", True), 2)
            self.add("A", {"b.dll": b"b"})
            self.assertEqual(app.toggle_cli(self.ctx, "A", True), 2)    # nombre repetido: hace falta el uid
        self.assertTrue(st.is_enabled(a.uid))
        self.assertFalse(deploy.fingerprint(self.game_dir) != self.before)   # no aplica nada


class BundledTest(Base):
    game_files = {"Game.exe": b"exe", "UnityPlayer.dll": b"u"}

    def test_install_bundled_twice_keeps_record(self):
        import shutil
        import subprocess
        if not shutil.which("bsdtar"):
            self.skipTest("hace falta bsdtar")
        src = self.tmp / "col"
        write(src, {"collection.json": b"{}", "bundled/ajustes/BepInEx/config/a.cfg": b"x=1"})
        arc = self.tmp / "col.zip"
        subprocess.run(["bsdtar", "-a", "-cf", str(arc), "-C", str(src), "collection.json", "bundled"], check=True)
        rec = manager.install_bundled(self.ctx, arc, "ajustes", "Ajustes", "1")
        self.assertEqual(len(rec.files), 1)
        target = rec.files[0][1]
        again = manager.install_bundled(self.ctx, arc, "ajustes", "Ajustes", "2")
        self.assertEqual((again.uid, len(self.ctx.state.mods)), (rec.uid, 1))
        manager.apply(self.ctx)
        self.assertEqual((self.game_dir / target).read_bytes(), b"x=1")
        self.assert_restored()


class GameUpdateTest(Base):
    game_files = {"Game.exe": b"exe"}

    def test_warns_after_steam_update(self):
        self.game.build = "100"
        self.add("A", {"a.dll": b"a"})
        self.assertFalse(self.ctx.game_updated())         # aún sin aplicar
        manager.apply(self.ctx)
        self.assertEqual(self.ctx.state.applied_build, "100")
        self.assertFalse(self.ctx.game_updated())
        self.game.build = "101"                            # Steam actualiza el juego
        self.assertTrue(self.ctx.game_updated())
        self.ctx.mark_build()                              # «Ya lo he revisado»
        self.assertFalse(self.ctx.game_updated())
        # Mods aplicados antes de que Crisol guardara la versión: se toma la actual, sin avisar.
        self.ctx.state.applied_build = ""
        self.assertFalse(self.ctx.game_updated())
        self.assertEqual(self.ctx.state.applied_build, "101")


class LoaderInstallTest(Base):
    game_files = {"Proj/Content/Paks/a.pak": b"p", "Proj/Binaries/Win64/Proj-Win64-Shipping.exe": b"e"}

    def test_ue4ss_goes_to_win64_first_and_restores(self):
        from unittest import mock
        from crisol import loaders
        z = make_zip(self.tmp / "UE4SS_v3.0.1.zip", {"dwmapi.dll": b"d", "UE4SS.dll": b"u", "Mods/mods.txt": b"m"})
        other = self.add("Pak", {"Pak_P.pak": b"x"})
        rel = {"tag": "v3.0.1", "url": "https://example/UE4SS_v3.0.1.zip", "name": z.name, "size": z.stat().st_size}
        def fake_download(url, dest, *a, **k):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(z.read_bytes())
            return dest
        with mock.patch.object(loaders, "release", return_value=rel), \
                mock.patch.object(manager, "download", side_effect=fake_download):
            rec = loaders.install(self.ctx, "ue4ss")
        self.assertEqual(self.ctx.state.profile.order, [rec.uid, other.uid])
        self.assertIn(["dwmapi.dll", "Proj/Binaries/Win64/dwmapi.dll"], rec.files)
        self.assertTrue(self.ctx.loader().installed)
        self.assertTrue(any("dwmapi=n,b" in n for n in self.ctx.plan().notes))
        # Si UE4SS no arranca en el juego (registro con error fatal), deja de contar como instalado.
        write(self.game_dir, {"Proj/Binaries/Win64/UE4SS.log": b"[x] Starting\n[y] Fatal Error: PS scan timed out\n"})
        ld = self.ctx.loader()
        self.assertFalse(ld.installed)
        self.assertIn("PS scan timed out", ld.detail)
        # Otra versión escribe en Win64/ue4ss/: vale el registro más reciente (aquí, sin errores).
        import os
        import time
        newer = self.game_dir / "Proj/Binaries/Win64/ue4ss/UE4SS.log"
        newer.parent.mkdir(parents=True, exist_ok=True)
        newer.write_bytes(b"[x] Starting Lua mod 'Keybinds'\n")
        os.utime(newer, (time.time() + 5, time.time() + 5))
        self.assertTrue(self.ctx.loader().installed)
        newer.unlink()
        newer.parent.rmdir()
        (self.game_dir / "Proj/Binaries/Win64/UE4SS.log").unlink()
        manager.apply(self.ctx)
        self.assertTrue((self.game_dir / "Proj/Binaries/Win64/UE4SS.dll").exists())
        self.assert_restored()


class UpdateCheckTest(Base):
    game_files = {"Game.exe": b"exe"}

    def test_page_version_is_only_a_hint(self):
        from unittest import mock
        from crisol.providers.base import FileInfo
        rec = self.add("UE4SS", {"a.dll": b"a"})
        rec.provider, rec.game_domain, rec.mod_id, rec.file_id, rec.version = "nexus", "g", 259, 886, "3.0.1"
        nexus = mock.Mock()
        nexus.latest_versions.return_value = {259: "1.0"}                     # la página no se actualizó
        nexus.files.return_value = [FileInfo(886, "UE4SS-LiesOp", "3.0.1", "MAIN", 1, "x.zip", date=100)]
        self.assertEqual(manager.check_updates(self.ctx, nexus), 0)
        self.assertFalse(rec.update_available)
        nexus.files.return_value.append(FileInfo(900, "UE4SS-LiesOp", "3.0.2", "MAIN", 1, "y.zip", date=200))
        self.assertEqual(manager.check_updates(self.ctx, nexus), 1)          # ahora sí hay archivo más nuevo
        self.assertEqual(rec.latest_version, "3.0.2")
        self.assertEqual(manager.update_target(nexus, rec).file_id, 900)


class NoExtensionTest(Base):
    game_files = {"Game.exe": b"exe"}

    def test_zip_without_extension_is_extracted(self):
        z = make_zip(self.tmp / "dl" / "0b_53_a2_0b53a286", {"Mod/BepInEx/x.dll": b"x", "Mod/y.dll": b"y"})
        rec = manager.install_archive(self.ctx, z, ModRecord(uid=self.ctx.state.new_uid(), name="M"))
        self.assertEqual(len(rec.files), 2)


class ME3Test(Base):
    game_files = {"Game/eldenring.exe": b"exe", "Game/regulation.bin": b"r"}

    def test_collection_settings_go_inside_their_mods(self):
        import shutil
        import subprocess
        if not shutil.which("bsdtar"):
            self.skipTest("hace falta bsdtar")
        fps = self.add("ERFPS", {"ERFPS/dll/erfps.dll": b"d", "ERFPS/dll/erfps_settings.ini": b"fov=60"})
        coop = self.add("Seamless", {"SeamlessCoop/ersc.dll": b"d", "SeamlessCoop/ersc_settings.ini": b"pw="})
        src = self.tmp / "col"
        write(src, {"collection.json": b"{}", "bundled/preset/dll/erfps_settings.ini": b"fov=90",
                    "bundled/preset/SeamlessCoop/ersc_settings.ini": b"pw=abc",
                    "bundled/preset/otro/nadie.ini": b"x"})
        arc = self.tmp / "col.zip"
        subprocess.run(["bsdtar", "-a", "-cf", str(arc), "-C", str(src), "collection.json", "bundled"], check=True)
        st = self.ctx.state
        rec = manager.install_bundled(self.ctx, arc, "preset", "Preset Settings")
        fps_ini = st.staging(fps.uid) / "ERFPS/dll/erfps_settings.ini"
        self.assertEqual(fps_ini.read_bytes(), b"fov=90")
        self.assertEqual((st.staging(coop.uid) / "SeamlessCoop/ersc_settings.ini").read_bytes(), b"pw=abc")
        self.assertEqual(rec.skipped, ["otro/nadie.ini"])
        manager.apply(self.ctx)
        self.assertNotIn(rec.uid, self.ctx.me3_profile.read_text())     # no es un paquete ME3
        self.assertNotIn(rec.uid, [st_uid for st_uid, _ in self.ctx.plan().files.values()])
        # Reinstalar ERFPS desde su descarga mantiene los ajustes de la colección.
        manager.reinstall(self.ctx, fps.uid)
        self.assertEqual(fps_ini.read_bytes(), b"fov=90")
        # Reinstalar el propio preset no pierde los originales.
        manager.reinstall(self.ctx, rec.uid)
        # Quitar el preset devuelve los originales.
        st.remove(rec.uid)
        self.assertEqual(fps_ini.read_bytes(), b"fov=60")
        self.assertEqual((st.staging(coop.uid) / "SeamlessCoop/ersc_settings.ini").read_bytes(), b"pw=")

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

    def test_launch_command_for_launchers(self):
        files = {"C/me3/c.me3": b'profileVersion = "v1"\n[[supports]]\ngame = "eldenring"\n[[package]]\nid = "c"\npath = "./../mod"\n',
                 "C/me3/Linux/me3": b"#!/bin/sh\n", "C/me3/Linux/win64/me3.exe": b"x", "C/mod/regulation.bin": b"c"}
        self.add("Conv", files)
        self.assertIsNone(manager.launch_command(self.ctx))          # sin perfil guardado: juego normal
        manager.apply(self.ctx)
        cmd = manager.launch_command(self.ctx)
        self.assertEqual(cmd[1:4], ["launch", "--game", "eldenring"])
        self.ctx.state.set_enabled(self.ctx.state.ordered()[0].uid, False)  # cambio sin guardar: se guarda antes
        self.assertIsNotNone(manager.launch_command(self.ctx))
        self.assertFalse(self.ctx.state.dirty_deploy)
        manager.restore(self.ctx)                                    # «Quitar perfil de ME3»: juego normal
        self.assertIsNone(manager.launch_command(self.ctx))

    def test_other_loaders_not_needed(self):
        from crisol.layouts import NotNeeded
        with self.assertRaises(NotNeeded):
            self.add("Elden Mod Loader", {"dinput8.dll": b"d", "mod_loader_config.ini": b"c", "mods/x.txt": b""})
        rec = self.add("AdjustTheFov", {"mods/AdjustTheFov.dll": b"d", "mods/AdjustTheFov/config.ini": b"c"})
        self.assertEqual(rec.natives, ["mods/AdjustTheFov.dll"])

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
        # Lanzado por Crisol en un juego de Steam con Gaming Deck: pasa por su perfil.
        from unittest import mock
        with mock.patch("shutil.which", side_effect=lambda n: "/usr/bin/gaming-deck" if n == "gaming-deck" else None):
            cmd = manager.me3_command(self.ctx, for_launch=True)
            self.assertEqual(cmd[:5], ["/usr/bin/gaming-deck", "run", "--profile", self.game.key, "--"])
            self.assertEqual(cmd[6:8], ["launch", "--game"])
            self.assertEqual(manager.me3_command(self.ctx)[1], "launch")  # la línea para Steam, sin envolver
        with mock.patch("shutil.which", return_value=None):
            self.assertEqual(manager.me3_command(self.ctx, for_launch=True)[1], "launch")


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


class SavesTest(Base):
    game_files = {"Game.exe": b"exe", "Game/Saved/SaveGames/1/slot.sav": b"v1"}

    def test_find_backup_restore(self):
        from crisol import saves
        pfx = self.tmp / "pfx"
        write(pfx, {"drive_c/users/steamuser/AppData/Roaming/Studio/G/steam_autocloud.vdf": b"x",
                    "drive_c/users/steamuser/AppData/Roaming/Studio/G/save0.dat": b"p1",
                    "drive_c/users/steamuser/AppData/Local/Temp/junk.sav": b"no"})
        self.game.prefix = pfx
        found = {(d.root, d.rel) for d in saves.find(self.game)}
        self.assertEqual(found, {("prefix", "AppData/Roaming/Studio/G"), ("game", "Game/Saved/SaveGames/1")})
        b = manager.backup_saves(self.ctx, "prueba")
        self.assertIsNotNone(b)
        self.assertIsNone(manager.backup_saves(self.ctx, "otra"))  # hace menos de 10 minutos
        (self.game_dir / "Game/Saved/SaveGames/1/slot.sav").write_bytes(b"v2-roto")
        n = saves.restore(self.game, b["id"])
        self.assertEqual(n, 3)
        self.assertEqual((self.game_dir / "Game/Saved/SaveGames/1/slot.sav").read_bytes(), b"v1")
        self.assertEqual(len(saves.backups(self.game)), 2)  # + la copia «antes de restaurar»
        # Restaurar la más antigua con el cupo lleno no la borra a mitad.
        for _ in range(saves.KEEP):
            saves.backup(self.game, "relleno")
        oldest = saves.backups(self.game)[-1]["id"]
        saves.restore(self.game, oldest)


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

    def test_ue4ss_from_nexus_and_files_by_name(self):
        write(self.game_dir, {"LiesofP/Content/Movies/Splash/intro_a.bk2": b"orig"})
        self.before = deploy.fingerprint(self.game_dir)
        ue = self.add("UE4SS for LoP", {"dwmapi.dll": b"d", "ue4ss/UE4SS.dll": b"u", "ue4ss/Mods/mods.txt": b"m"})
        self.assertIn(["dwmapi.dll", "LiesofP/Binaries/Win64/dwmapi.dll"], ue.files)
        self.assertIn(["ue4ss/UE4SS.dll", "LiesofP/Binaries/Win64/ue4ss/UE4SS.dll"], ue.files)
        intro = self.add("No Intro", {"intro_a.bk2": b"vacio"})       # sin carpetas: se busca por nombre
        self.assertEqual(intro.files, [["intro_a.bk2", "LiesofP/Content/Movies/Splash/intro_a.bk2"]])
        manager.apply(self.ctx)
        self.assertEqual((self.game_dir / "LiesofP/Content/Movies/Splash/intro_a.bk2").read_bytes(), b"vacio")
        self.assert_restored()

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
