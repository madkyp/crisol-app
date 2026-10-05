"""Punto de entrada: aplicación GTK de instancia única que también recibe enlaces nxm://."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, Gtk  # noqa: E402

from . import APP_ID, APP_NAME, VERSION, paths  # noqa: E402
from .i18n import _  # noqa: E402

HERE = Path(__file__).resolve().parent
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
    ap.add_argument("--game", metavar="JUEGO", help=_("abrir directamente la página de un juego (clave de --list)"))
    ap.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    ap.add_argument("uri", nargs="?", help=_("enlace nxm:// (lo pasa el navegador)"))
    args, _u = ap.parse_known_args(argv[1:])
    setup_logging(args.debug)
    global _OPEN_GAME
    _OPEN_GAME = args.game
    if args.list or args.restore or args.play:
        from . import manager
        from .controller import Controller
        ctl = Controller()
        games = ctl.scan(refresh=False)  # sin esperar a la red: es para otras apps (Gaming Deck, Umbral)
        if args.list:
            print(json.dumps([game_status(ctl, g) for g in games], indent=1, ensure_ascii=False))
            return 0
        key = args.restore or args.play
        game = next((g for g in games if g.key == key), None)
        if not game:
            print(_("No existe el juego {0}").format(key), file=sys.stderr)
            return 2
        ctx = manager.context(game)
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
            "loader": {"name": ld.name, "level": ld.level, "installed": ld.installed} if ld else None}


def play(ctx) -> int:
    """Lanza el juego con sus mods. Con ME3, el perfil de Crisol; si no, los archivos ya están en el juego."""
    import shutil
    import subprocess
    from . import manager
    game = ctx.game
    if ctx.layout.external:
        if ctx.state.dirty_deploy or not ctx.is_applied():
            manager.apply(ctx)  # guardar el perfil de ME3 con lo activo ahora
        cmd = manager.me3_command(ctx)
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


class CrisolApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.win = None
        self._pending: list[str] = []
        self.accent_css = Gtk.CssProvider()

    def do_startup(self):
        Adw.Application.do_startup(self)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        display = Gdk.Display.get_default()
        css = Gtk.CssProvider()
        css.load_from_path(str(HERE / "ui" / "style.css"))
        Gtk.StyleContext.add_provider_for_display(display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Gtk.StyleContext.add_provider_for_display(display, self.accent_css, Gtk.STYLE_PROVIDER_PRIORITY_USER + 1)
        quit_ = Gio.SimpleAction.new("quit", None)
        quit_.connect("activate", lambda *_u: self.quit())
        self.add_action(quit_)
        self.set_accels_for_action("app.quit", ["<Ctrl>q"])

    def set_accent(self, color: str) -> None:
        # Los temas de GTK del usuario (p. ej. Catppuccin de HyDE) redefinen el acento con prioridad de
        # usuario; el de Crisol va por encima para que la opción de Preferencias se respete.
        self.accent_css.load_from_string(
            f"@define-color accent_bg_color {color};\n@define-color accent_color {color};\n"
            f"@define-color accent_fg_color white;\n"
            f":root {{ --accent-bg-color: {color}; --accent-fg-color: white; }}\n"
            # El tema puede pintar estos widgets con colores fijos en vez de con la variable.
            f"button.suggested-action, splitbutton.suggested-action > button {{ background-color: {color}; color: white; }}\n"
            f"button.suggested-action:hover {{ background-color: color-mix(in srgb, {color} 88%, white); }}\n"
            f"button.suggested-action:active {{ background-color: color-mix(in srgb, {color} 80%, black); }}\n"
            f"button.suggested-action:disabled {{ background-color: alpha({color}, 0.35); color: alpha(white, 0.5); }}\n"
            f"switch:checked {{ background-color: {color}; }}\n"
            f"check:checked, radio:checked {{ background-color: {color}; color: white; }}\n"
            f"progressbar > trough > progress {{ background-color: {color}; }}\n"
            f"spinner, .spinner {{ color: {color}; }}\n")

    def _window(self):
        if self.win is None:
            from .controller import Controller
            from .ui.window import MainWindow
            ctl = Controller()
            self.set_accent(ctl.cfg.accent)
            self.win = MainWindow(self, ctl, open_game=_OPEN_GAME)
        return self.win

    def do_activate(self):
        self._window().present()

    def do_open(self, files, n_files, hint):
        win = self._window()
        win.present()
        for f in files:
            uri = f.get_uri()
            if uri.lower().startswith("nxm:"):
                win.handle_nxm(uri)


def main(argv: list[str]) -> int:
    code = cli(argv)
    if code is not None:
        return code
    # GApplication no entiende nuestras opciones: solo se le pasa el enlace nxm, si lo hay.
    passthrough = [argv[0]] + [a for a in argv[1:] if a.lower().startswith("nxm:")]
    return CrisolApp().run(passthrough)
