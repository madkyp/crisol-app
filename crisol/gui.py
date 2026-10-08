"""Aplicación GTK de instancia única que también recibe enlaces nxm:// (solo para la ventana)."""
from __future__ import annotations

from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID  # noqa: E402

HERE = Path(__file__).resolve().parent


class CrisolApp(Adw.Application):
    def __init__(self, open_game: str | None = None):
        self.open_game = open_game
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
        # Para las notificaciones: mostrar la ventana o ir a la página de un juego.
        show = Gio.SimpleAction.new("show", None)
        show.connect("activate", lambda *_u: self._window().present())
        self.add_action(show)
        game = Gio.SimpleAction.new("open-game", GLib.VariantType.new("s"))
        game.connect("activate", lambda _a, key: self._open_game(key.get_string()))
        self.add_action(game)

    def _open_game(self, key: str) -> None:
        win = self._window()
        win.present()
        g = next((g for g in win.ctl.games if g.key == key), None)
        if g:
            page = win.current_game_page()
            if not page or page.game.key != key:
                win.open_game(g)

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
            self.win = MainWindow(self, ctl, open_game=self.open_game)
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
