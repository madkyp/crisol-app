"""Preferencias: API key de Nexus Mods, enlaces nxm y apariencia."""
from __future__ import annotations

from gi.repository import Adw, GLib, Gtk

from .. import nxm_handler
from .util import run_async

ACCENTS = [("Brasa", "#e0703a"), ("Azul", "#3584e4"), ("Verde", "#2ec27e"), ("Violeta", "#9141ac"),
           ("Rosa", "#e66198"), ("Ámbar", "#e5a50a"), ("Turquesa", "#2190a4")]
KEYS_URL = "https://www.nexusmods.com/users/myaccount?tab=api%20access"


class PrefsDialog(Adw.PreferencesDialog):
    def __init__(self, win):
        super().__init__(title="Preferencias")
        self.win = win
        self.ctl = win.ctl
        page = Adw.PreferencesPage(title="General", icon_name="preferences-system-symbolic")

        nx = Adw.PreferencesGroup(
            title="Nexus Mods",
            description="Tu API key personal se guarda en el llavero del sistema (libsecret), no en un archivo. "
                        "Buscar mods no la necesita; descargar sí.")
        self.key = Adw.PasswordEntryRow(title="API key personal", show_apply_button=True)
        if self.ctl.nexus.api_key:
            self.key.set_text(self.ctl.nexus.api_key)
        self.key.connect("apply", self._save_key)
        nx.add(self.key)
        self.account = Adw.ActionRow(title="Cuenta")
        self.account.add_css_class("property")
        nx.add(self.account)
        get = Adw.ActionRow(title="Conseguir la API key", subtitle="nexusmods.com → Preferencias → API → Personal API Key",
                            activatable=True)
        get.add_suffix(Gtk.Image.new_from_icon_name("adw-external-link-symbolic"))
        get.connect("activated", lambda *_: Gtk.UriLauncher.new(KEYS_URL).launch(win, None, None))
        nx.add(get)
        forget = Adw.ButtonRow(title="Borrar la API key")
        forget.add_css_class("destructive-action")
        forget.connect("activated", self._forget)
        nx.add(forget)
        page.add(nx)

        links = Adw.PreferencesGroup(
            title="Enlaces de descarga",
            description="Sin Nexus Premium, las descargas empiezan en la web con «Mod Manager Download» / «Slow "
                        "download»; el navegador pasa un enlace nxm:// a la app registrada para ellos.")
        self.nxm = Adw.ActionRow(title="Enlaces nxm://")
        self.nxm_btn = Gtk.Button(label="Usar Crisol", valign=Gtk.Align.CENTER, css_classes=["pill"])
        self.nxm_btn.connect("clicked", self._register)
        self.nxm.add_suffix(self.nxm_btn)
        links.add(self.nxm)
        page.add(links)

        look = Adw.PreferencesGroup(title="Apariencia")
        colors = [c for _, c in ACCENTS]
        sel = colors.index(self.ctl.cfg.accent) if self.ctl.cfg.accent in colors else 0
        self.accent = Adw.ComboRow(title="Color de acento", model=Gtk.StringList.new([n for n, _ in ACCENTS]),
                                   selected=sel)
        self.accent.connect("notify::selected", self._accent)
        look.add(self.accent)
        show = Adw.SwitchRow(title="Mostrar todos los juegos",
                             subtitle="También los que no tienen mods en Nexus Mods",
                             active=self.ctl.cfg.show_all_games)
        show.connect("notify::active", self._show_all)
        look.add(show)
        adult = Adw.SwitchRow(title="Mostrar mods para adultos", subtitle="Nexus los marca como tales; ocultos por defecto",
                              active=self.ctl.cfg.show_adult)
        adult.connect("notify::active", self._adult)
        look.add(adult)
        page.add(look)
        self.add(page)
        self._refresh_account()
        self._refresh_nxm()

    def _refresh_account(self) -> None:
        a = self.ctl.account
        if not self.ctl.nexus.api_key:
            self.account.set_subtitle("Sin API key")
        elif a:
            self.account.set_subtitle(GLib.markup_escape_text(
                f"{a.get('name', '?')} · {'Premium' if a.get('is_premium') else 'Gratuita (descargas desde la web)'}"))
        else:
            self.account.set_subtitle(GLib.markup_escape_text(self.ctl.account_error or "Comprobando…"))

    def _refresh_nxm(self) -> None:
        cur = nxm_handler.current()
        if cur == nxm_handler.DESKTOP:
            self.nxm.set_subtitle("Crisol recibe los enlaces")
            self.nxm_btn.set_visible(False)
        else:
            self.nxm.set_subtitle(GLib.markup_escape_text(f"Ahora los abre: {cur}" if cur else "Ninguna app los abre"))
            self.nxm_btn.set_visible(True)

    def _save_key(self, row) -> None:
        key = row.get_text().strip()
        self.ctl.set_api_key(key or None)
        self.account.set_subtitle("Comprobando…")

        def done(_a):
            self._refresh_account()
            if self.ctl.account:
                self.add_toast(Adw.Toast(title="API key guardada y válida"))
            self.win._account_checked()
        run_async(self.ctl.validate_account, done)

    def _forget(self, *_):
        self.ctl.set_api_key(None)
        self.ctl.account = None
        self.key.set_text("")
        self._refresh_account()

    def _register(self, *_):
        try:
            nxm_handler.register()
        except Exception as e:  # noqa: BLE001
            self.add_toast(Adw.Toast(title=GLib.markup_escape_text(f"No se pudo registrar: {e}")))
        self._refresh_nxm()

    def _accent(self, row, _p):
        color = ACCENTS[row.get_selected()][1]
        self.ctl.cfg.accent = color
        self.ctl.cfg.save()
        self.win.get_application().set_accent(color)

    def _adult(self, row, _p):
        self.ctl.cfg.show_adult = self.ctl.nexus.show_adult = row.get_active()
        self.ctl.cfg.save()
        page = self.win.current_game_page()
        if page:
            page.search(reset=True)

    def _show_all(self, row, _p):
        self.ctl.cfg.show_all_games = row.get_active()
        self.ctl.cfg.save()
        self.win.library.show_all.set_active(row.get_active())
