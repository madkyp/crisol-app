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
    ap = argparse.ArgumentParser(prog="crisol", description=f"{APP_NAME}: gestor de mods para Steam y Umbral")
    ap.add_argument("--debug", action="store_true", help="registro detallado en la terminal")
    ap.add_argument("--list", action="store_true", help="juegos detectados y su juego en Nexus Mods, en JSON")
    ap.add_argument("--restore", metavar="JUEGO", help="quitar los mods de un juego (clave de --list) sin abrir la ventana")
    ap.add_argument("--game", metavar="JUEGO", help="abrir directamente la página de un juego (clave de --list)")
    ap.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    ap.add_argument("uri", nargs="?", help="enlace nxm:// (lo pasa el navegador)")
    args, _ = ap.parse_known_args(argv[1:])
    setup_logging(args.debug)
    global _OPEN_GAME
    _OPEN_GAME = args.game
    if args.list or args.restore:
        from . import manager
        from .controller import Controller
        ctl = Controller()
        games = ctl.scan()
        if args.list:
            print(json.dumps([{"key": g.key, "name": g.name, "source": g.source, "dir": str(g.install_dir),
                               "nexus": ctl.domain(g), "mods": len(manager.context(g).state.mods),
                               "deployed": manager.context(g).deployer.is_deployed()} for g in games],
                             indent=1, ensure_ascii=False))
            return 0
        game = next((g for g in games if g.key == args.restore), None)
        if not game:
            print(f"No existe el juego {args.restore}", file=sys.stderr)
            return 2
        r = manager.restore(manager.context(game))
        print(f"Quitados {r.removed} archivos, restaurados {r.restored} originales"
              + (f"; cambiados por otro programa (se dejan): {', '.join(r.kept_changed)}" if r.kept_changed else ""))
        return 0
    return None


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
        quit_.connect("activate", lambda *_: self.quit())
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
