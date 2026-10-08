"""Punto de entrada: órdenes de terminal y, si no hay ninguna, la ventana (gui.py).

Las órdenes sin ventana (--list, --play, --restore, --launch-command) no cargan GTK: Gaming Deck llama a
--launch-command desde el entorno de Steam, cuyas librerías antiguas (LD_LIBRARY_PATH) rompen gi."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import logging
import sys

from . import APP_NAME, VERSION, paths
from .i18n import _

_OPEN_GAME: str | None = None
log = logging.getLogger("crisol")


def setup_logging(debug: bool) -> None:
    paths.ensure_dirs()
    handlers: list[logging.Handler] = [logging.FileHandler(paths.LOG_DIR / "crisol.log", mode="w")]
    if debug:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO, handlers=handlers, force=True,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def cli(argv: list[str]) -> int | None:
    """Órdenes sin interfaz. Devuelve el código de salida, o None para abrir la ventana."""
    ap = argparse.ArgumentParser(prog="crisol", description=_("{0}: gestor de mods para Steam y Umbral").format(APP_NAME))
    ap.add_argument("--debug", action="store_true", help=_("registro detallado en la terminal"))
    ap.add_argument("--list", action="store_true", help=_("juegos detectados y su juego en Nexus Mods, en JSON"))
    ap.add_argument("--restore", metavar="JUEGO", help=_("quitar los mods de un juego (clave de --list) sin abrir la ventana"))
    ap.add_argument("--play", metavar="JUEGO", help=_("jugar con los mods: ME3 en juegos de FromSoftware, Umbral en "
                                                      "los suyos, Steam en el resto"))
    ap.add_argument("--launch-command", metavar="JUEGO",
                    help=_("para lanzadores (Gaming Deck): la orden de ME3 en JSON si el juego tiene un perfil de "
                           "ME3 aplicado; código 1 si se arranca normal"))
    ap.add_argument("--export", metavar="JUEGO", help=_("lista de mods de un juego (para importarla en otro PC), "
                                                         "en JSON por la salida estándar"))
    ap.add_argument("--apply", metavar="JUEGO", help=_("aplicar los mods del perfil activo sin abrir la ventana "
                                                        "(resultado en JSON; código 3 si el juego está abierto)"))
    ap.add_argument("--enable", nargs=2, metavar=("JUEGO", "MOD"),
                    help=_("activar un mod en el perfil activo (MOD: uid de --export o nombre exacto); no aplica"))
    ap.add_argument("--disable", nargs=2, metavar=("JUEGO", "MOD"),
                    help=_("desactivar un mod en el perfil activo (MOD: uid de --export o nombre exacto); no aplica"))
    ap.add_argument("--profile", metavar="NOMBRE", help=_("con --apply: cambiar antes a ese perfil"))
    ap.add_argument("--check-updates", metavar="JUEGO", nargs="?", const="all",
                    help=_("buscar actualizaciones de mods en Nexus (de un juego o de todos), en JSON"))
    ap.add_argument("--notify", action="store_true", help=_("con --check-updates: avisar con una notificación "
                                                             "si hay mods con versión nueva"))
    ap.add_argument("--backup", metavar="ARCHIVO", help=_("copia de seguridad de los datos de Crisol (.tar.gz; sin "
                                                           "los mods ni la API key)"))
    ap.add_argument("--with-saves", action="store_true", help=_("con --backup: incluir las copias de partidas"))
    ap.add_argument("--restore-backup", metavar="ARCHIVO", help=_("restaurar una copia de seguridad de Crisol"))
    ap.add_argument("--game", metavar="JUEGO", help=_("abrir directamente la página de un juego (clave de --list)"))
    ap.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    ap.add_argument("uri", nargs="?", help=_("enlace nxm:// (lo pasa el navegador)"))
    args, _u = ap.parse_known_args(argv[1:])
    setup_logging(args.debug)
    global _OPEN_GAME
    _OPEN_GAME = args.game
    if args.launch_command:
        from . import games as _games
        from . import manager
        # Rápido y sin red: Gaming Deck lo llama en cada lanzamiento desde Steam.
        game = next((g for g in _games.scan_all() if g.key == args.launch_command), None)
        cmd = manager.launch_command(manager.context(game)) if game else None
        if not cmd:
            return 1
        manager.backup_saves(manager.context(game), _("antes de jugar con mods"))
        print(json.dumps(cmd, ensure_ascii=False))
        return 0
    if args.backup or args.restore_backup:
        from . import backup
        try:
            if args.backup:
                m = backup.create(Path(args.backup).expanduser(), args.with_saves)
                print(_("Copia guardada: {0} juegos en {1}").format(len(m["games"]), args.backup))
            else:
                r = backup.restore(Path(args.restore_backup).expanduser())
                print(json.dumps({"restored": r.games, "lists_to_import": r.lists, "saves_added": r.saves,
                                  "previous_data": r.safety}, indent=1, ensure_ascii=False))
        except (backup.BackupError, OSError) as e:
            print(e, file=sys.stderr)
            return 1
        return 0
    toggle = args.enable or args.disable
    if args.list or args.restore or args.play or args.export or args.apply or args.check_updates or toggle:
        from . import manager
        from .controller import Controller
        ctl = Controller()
        games = ctl.scan(refresh=False)  # sin esperar a la red: es para otras apps (Gaming Deck, Umbral)
        if args.list:
            print(json.dumps([game_status(ctl, g) for g in games], indent=1, ensure_ascii=False))
            return 0
        if args.check_updates:
            return check_updates_cli(ctl, games, args.check_updates, args.notify)
        key = args.restore or args.play or args.export or args.apply or (toggle[0] if toggle else None)
        game = next((g for g in games if g.key == key), None)
        if not game:
            print(_("No existe el juego {0}").format(key), file=sys.stderr)
            return 2
        ctx = manager.context(game)
        if toggle:
            return toggle_cli(ctx, toggle[1], bool(args.enable))
        if args.apply:
            return apply_cli(ctx, args.profile)
        if args.export:
            from . import modlist
            print(json.dumps(modlist.export(ctx), indent=1, ensure_ascii=False))
            return 0
        if args.play:
            return play(ctx)
        r = manager.restore(ctx)
        print(_("Quitados {0} archivos, restaurados {1} originales").format(r.removed, r.restored)
              + (_("; cambiados por otro programa (se dejan): {0}").format(", ".join(r.kept_changed))
                 if r.kept_changed else ""))
        return 0
    return None


def game_status(ctl, g) -> dict:
    """Estado de un juego para otras apps (formato estable: ver «Integration» en el README)."""
    from . import manager
    ctx = manager.context(g)
    st = ctx.state
    ld = ctx.loader()
    return {"key": g.key, "name": g.name, "source": g.source, "id": g.source_id, "dir": str(g.install_dir),
            "nexus": ctl.domain(g), "layout": ctx.layout.id, "mods": len(st.mods), "enabled": len(st.profile.enabled),
            "profile": st.active, "applied": ctx.is_applied(), "pending_changes": st.dirty_deploy and bool(st.mods),
            "updates": sum(1 for m in st.mods.values() if m.update_available),
            "game_updated": ctx.game_updated(), "profiles": list(st.profiles),
            "updates_checked": int(st.updates_checked),
            "loader": {"name": ld.name, "level": ld.level, "installed": ld.installed} if ld else None}


def apply_cli(ctx, profile: str | None) -> int:
    """--apply: 0 bien, 2 perfil inexistente, 3 juego abierto, 1 otro error. Siempre JSON en la salida."""
    from . import manager
    from .running import GameRunning
    st = ctx.state
    if profile:
        if profile not in st.profiles:
            print(json.dumps({"ok": False, "error": _("No existe el perfil {0}").format(profile)}, ensure_ascii=False))
            return 2
        st.switch(profile)
        st.save()
    try:
        r = manager.apply(ctx)
    except GameRunning as e:
        print(json.dumps({"ok": False, "error": str(e), "running": True}, ensure_ascii=False))
        return 3
    except Exception as e:  # noqa: BLE001 — se informa a quien llama
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, "profile": st.active, "placed": r.placed, "removed": r.removed,
                      "restored": r.restored, "method": r.method, "notes": r.notes}, ensure_ascii=False))
    return 0


def notify_updates(game_key: str, game_name: str, mods: list[str]) -> bool:
    """Aviso de mods con versión nueva (una vez por cada lista distinta)."""
    from . import notify
    body = ", ".join(sorted(mods)[:4]) + ("…" if len(mods) > 4 else "")
    title = (_("{0}: {1} mods con versión nueva") if len(mods) > 1 else _("{0}: 1 mod con versión nueva")).format(
        game_name, len(mods))
    return notify.send(title, body, key=f"updates-{game_key}", game_key=game_key,
                       once=(f"updates:{game_key}", "|".join(sorted(mods))))


def toggle_cli(ctx, mod: str, on: bool) -> int:
    """--enable/--disable: solo cambia el perfil activo (como la casilla de la ventana); los archivos del juego
    no se tocan hasta --apply. 0 bien, 1 error, 2 no existe el mod (o el nombre es ambiguo)."""
    st = ctx.state
    rec = st.mods.get(mod)
    if rec is None:
        same = [m for m in st.mods.values() if m.name == mod]
        if len(same) > 1:
            print(json.dumps({"ok": False, "error": _("Hay varios mods con ese nombre: usa el uid de --export")},
                             ensure_ascii=False))
            return 2
        rec = same[0] if same else None
    if rec is None:
        print(json.dumps({"ok": False, "error": _("No existe el mod {0}").format(mod)}, ensure_ascii=False))
        return 2
    try:
        st.set_enabled(rec.uid, on)
        st.save()
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, "uid": rec.uid, "name": rec.name, "enabled": st.is_enabled(rec.uid),
                      "profile": st.active, "pending_changes": st.dirty_deploy}, ensure_ascii=False))
    return 0


def check_updates_cli(ctl, games, which: str, notify_user: bool = False) -> int:
    from . import manager
    out = {}
    for g in games:
        if which != "all" and g.key != which:
            continue
        ctx = manager.context(g)
        if not any(m.provider == "nexus" for m in ctx.state.mods.values()):
            continue
        try:
            out[g.key] = {"name": g.name, "updates": manager.check_updates(ctx, ctl.nexus),
                          "mods": [m.name for m in ctx.state.mods.values() if m.update_available]}
        except Exception as e:  # noqa: BLE001
            out[g.key] = {"name": g.name, "error": str(e)}
    if notify_user:
        for key, v in out.items():
            if v.get("mods"):
                notify_updates(key, v["name"], v["mods"])
    print(json.dumps(out, indent=1, ensure_ascii=False))
    return 0 if all("error" not in v for v in out.values()) else 1


def play(ctx) -> int:
    """Lanza el juego con sus mods. Con ME3, el perfil de Crisol; si no, los archivos ya están en el juego."""
    import shutil
    import subprocess
    from . import manager
    game = ctx.game
    if ctx.layout.external and game.source == "steam" and manager.deck_launches_me3():
        # A través de Steam: Steam Input configura el mando y Gaming Deck arranca ME3 con su perfil.
        if ctx.state.dirty_deploy or not ctx.is_applied():
            manager.apply(ctx)
        cmd = ["xdg-open", f"steam://rungameid/{game.source_id}"]
    elif ctx.layout.external:
        if ctx.state.dirty_deploy or not ctx.is_applied():
            manager.apply(ctx)  # guardar el perfil de ME3 con lo activo ahora
        cmd = manager.me3_command(ctx, for_launch=True)
        if not cmd:
            print(_("Falta Mod Engine 3 (ME3)"), file=sys.stderr)
            return 1
        manager.backup_saves(ctx, _('antes de jugar con mods'))
    elif game.source == "umbral":
        if not shutil.which("umbral"):
            print(_("Umbral no está instalado"), file=sys.stderr)
            return 1
        cmd = ["umbral", "--launch", game.source_id]
    else:
        cmd = ["xdg-open", f"steam://rungameid/{game.source_id}"]
    if not ctx.layout.external and ctx.state.dirty_deploy and ctx.state.mods:
        print(_("Aviso: hay cambios de mods sin aplicar; se juega con lo último aplicado."), file=sys.stderr)
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return 0


def main(argv: list[str]) -> int:
    code = cli(argv)
    if code is not None:
        return code
    # GApplication no entiende nuestras opciones: solo se le pasa el enlace nxm, si lo hay.
    passthrough = [argv[0]] + [a for a in argv[1:] if a.lower().startswith("nxm:")]
    from .gui import CrisolApp  # GTK solo para la ventana (ver gui.py)
    return CrisolApp(_OPEN_GAME).run(passthrough)
