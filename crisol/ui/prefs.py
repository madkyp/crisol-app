"""Preferencias: API key de Nexus Mods, enlaces nxm y apariencia."""
from __future__ import annotations

from gi.repository import Adw, GLib, Gtk

from .. import nxm_handler, secrets
from .util import run_async
from ..i18n import _

ACCENTS = [(_('Brasa'), "#e0703a"), (_('Azul'), "#3584e4"), (_('Verde'), "#2ec27e"), (_('Violeta'), "#9141ac"),
           (_('Rosa'), "#e66198"), (_('Ámbar'), "#e5a50a"), (_('Turquesa'), "#2190a4")]
KEYS_URL = "https://www.nexusmods.com/users/myaccount?tab=api%20access"


class PrefsDialog(Adw.PreferencesDialog):
    def __init__(self, win):
        super().__init__(title=_('Preferencias'))
        self.win = win
        self.ctl = win.ctl
        page = Adw.PreferencesPage(title=_('General'), icon_name="preferences-system-symbolic")

        nx = Adw.PreferencesGroup(
            title="Nexus Mods",
            description=_('Tu API key personal se guarda en el llavero del sistema (Secret Service). Si no hay llavero, en un archivo que solo tu usuario puede leer. Buscar mods no la necesita; descargar sí.'))
        self.key = Adw.PasswordEntryRow(title=_('API key personal'), show_apply_button=True)
        if self.ctl.nexus.api_key:
            self.key.set_text(self.ctl.nexus.api_key)
        self.key.connect("apply", self._save_key)
        nx.add(self.key)
        self.account = Adw.ActionRow(title=_('Cuenta'))
        self.account.add_css_class("property")
        nx.add(self.account)
        self.where = Adw.ActionRow(title=_('Guardada en'))
        self.where.add_css_class("property")
        nx.add(self.where)
        get = Adw.ActionRow(title=_('Conseguir la API key'), subtitle="nexusmods.com → Preferencias → API → Personal API Key",
                            activatable=True)
        get.add_suffix(Gtk.Image.new_from_icon_name("adw-external-link-symbolic"))
        get.connect("activated", lambda *_u: Gtk.UriLauncher.new(KEYS_URL).launch(win, None, None))
        nx.add(get)
        forget = Adw.ButtonRow(title=_('Borrar la API key'))
        forget.add_css_class("destructive-action")
        forget.connect("activated", self._forget)
        nx.add(forget)
        page.add(nx)

        links = Adw.PreferencesGroup(
            title=_('Enlaces de descarga'),
            description=_('Sin Nexus Premium, las descargas empiezan en la web con «Mod Manager Download» / «Slow download»; el navegador pasa un enlace nxm:// a la app registrada para ellos.'))
        self.nxm = Adw.ActionRow(title=_('Enlaces nxm://'))
        self.nxm_btn = Gtk.Button(label=_('Usar Crisol'), valign=Gtk.Align.CENTER, css_classes=["pill"])
        self.nxm_btn.connect("clicked", self._register)
        self.nxm.add_suffix(self.nxm_btn)
        links.add(self.nxm)
        page.add(links)

        disk = Adw.PreferencesGroup(title=_('Espacio en disco'))
        keep = Adw.SwitchRow(title=_('Guardar los archivos descargados'),
                             subtitle=_('Permiten reinstalar un mod sin volver a descargarlo, pero ocupan tanto como el mod extraído (p. ej. The Convergence: ~10 GB más)'),
                             active=self.ctl.cfg.keep_archives)
        keep.connect("notify::active", self._keep)
        disk.add(keep)
        page.add(disk)

        look = Adw.PreferencesGroup(title=_('Apariencia'))
        colors = [c for _u, c in ACCENTS]
        sel = colors.index(self.ctl.cfg.accent) if self.ctl.cfg.accent in colors else 0
        self.accent = Adw.ComboRow(title=_('Color de acento'), model=Gtk.StringList.new([n for n, _u in ACCENTS]),
                                   selected=sel)
        self.accent.connect("notify::selected", self._accent)
        look.add(self.accent)
        from ..i18n import LANGUAGES
        self._langs = list(LANGUAGES)
        lang = Adw.ComboRow(title=_("Idioma"), subtitle=_("Se aplica al volver a abrir Crisol"),
                            model=Gtk.StringList.new([_(v) if k == "" else v for k, v in LANGUAGES.items()]),
                            selected=self._langs.index(self.ctl.cfg.language) if self.ctl.cfg.language in self._langs else 0)
        lang.connect("notify::selected", self._lang)
        look.add(lang)
        show = Adw.SwitchRow(title=_('Mostrar todos los juegos'),
                             subtitle=_('También los que no tienen mods en Nexus Mods'),
                             active=self.ctl.cfg.show_all_games)
        show.connect("notify::active", self._show_all)
        look.add(show)
        adult = Adw.SwitchRow(title=_('Mostrar mods para adultos'), subtitle=_('Nexus los marca como tales; ocultos por defecto'),
                              active=self.ctl.cfg.show_adult)
        adult.connect("notify::active", self._adult)
        look.add(adult)
        page.add(look)
        self.add(page)
        self._refresh_account()
        self._refresh_nxm()

    def _refresh_account(self) -> None:
        where = secrets.storage()
        self.where.set_visible(bool(where))
        if where == "keyring":
            self.where.set_subtitle(_('Llavero del sistema'))
        elif where == "file":
            self.where.set_subtitle(GLib.markup_escape_text(
                _("{0} (permisos 600). No hay llavero del sistema: instala y activa gnome-keyring o el Secret "
                  "Service de KWallet si prefieres guardarla ahí.").format(secrets.KEY_FILE)))
            self.where.set_subtitle_lines(3)
        a = self.ctl.account
        if not self.ctl.nexus.api_key:
            self.account.set_subtitle(_('Sin API key'))
        elif a:
            self.account.set_subtitle(GLib.markup_escape_text(
                f"{a.get('name', '?')} · {'Premium' if a.get('is_premium') else 'Gratuita (descargas desde la web)'}"))
        else:
            self.account.set_subtitle(GLib.markup_escape_text(self.ctl.account_error or _('Comprobando…')))

    def _refresh_nxm(self) -> None:
        cur = nxm_handler.current()
        if cur == nxm_handler.DESKTOP:
            self.nxm.set_subtitle(_('Crisol recibe los enlaces'))
            self.nxm_btn.set_visible(False)
        else:
            self.nxm.set_subtitle(GLib.markup_escape_text(_('Ahora los abre: {0}').format(cur) if cur else _('Ninguna app los abre')))
            self.nxm_btn.set_visible(True)

    def _save_key(self, row) -> None:
        key = row.get_text().strip()
        try:
            self.ctl.set_api_key(key or None)
        except Exception as e:  # noqa: BLE001 — se muestra, no se pierde
            self.add_toast(Adw.Toast(title=GLib.markup_escape_text(_('No se pudo guardar la clave: {0}').format(e)), timeout=8))
            return
        self.account.set_subtitle(_('Comprobando…'))

        def done(_a):
            self._refresh_account()
            if self.ctl.account:
                self.add_toast(Adw.Toast(title=_('API key guardada y válida')))
            self.win._account_checked()
        run_async(self.ctl.validate_account, done)

    def _forget(self, *_u):
        try:
            self.ctl.set_api_key(None)
        except Exception as e:  # noqa: BLE001
            self.add_toast(Adw.Toast(title=GLib.markup_escape_text(_('No se pudo borrar: {0}').format(e))))
        self.ctl.account = None
        self.key.set_text("")
        self._refresh_account()

    def _register(self, *_u):
        try:
            nxm_handler.register()
        except Exception as e:  # noqa: BLE001
            self.add_toast(Adw.Toast(title=GLib.markup_escape_text(_('No se pudo registrar: {0}').format(e))))
        self._refresh_nxm()

    def _accent(self, row, _p):
        color = ACCENTS[row.get_selected()][1]
        self.ctl.cfg.accent = color
        self.ctl.cfg.save()
        self.win.get_application().set_accent(color)

    def _lang(self, row, _p):
        self.ctl.cfg.language = self._langs[row.get_selected()]
        self.ctl.cfg.save()
        self.add_toast(Adw.Toast(title=_("El idioma cambiará al volver a abrir Crisol")))

    def _keep(self, row, _p):
        self.ctl.cfg.keep_archives = row.get_active()
        self.ctl.cfg.save()

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
