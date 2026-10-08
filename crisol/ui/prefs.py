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

        from .. import notify
        notif = Adw.PreferencesGroup(title=_('Avisos'))
        on = Adw.SwitchRow(title=_('Notificaciones del escritorio'),
                           subtitle=_('Mod o colección instalados, mods con versión nueva y juegos actualizados '
                                      'desde que aplicaste los mods'),
                           active=self.ctl.cfg.notifications)
        on.connect("notify::active", self._notifications)
        notif.add(on)
        bg = Adw.SwitchRow(title=_('Buscar actualizaciones con Crisol cerrado'),
                           subtitle=_('Cada 12 horas (temporizador de systemd de tu usuario); avisa con una notificación'),
                           active=notify.background_checks_on())
        bg.connect("notify::active", self._background)
        notif.add(bg)
        page.add(notif)

        from .. import backup
        from .util import human_size
        bk = Adw.PreferencesGroup(title=_('Copia de seguridad'),
                                  description=_('Ajustes, perfiles, orden, notas y listas de mods de todos los juegos, '
                                                'en un archivo pequeño. No lleva los mods (se vuelven a bajar) ni tu '
                                                'API key.'))
        self.with_saves = Adw.SwitchRow(title=_('Incluir las copias de partidas'),
                                        subtitle=human_size(backup.saves_size()))
        bk.add(self.with_saves)
        for title, sub, cb in ((_('Crear copia…'), _('Guardar los datos de Crisol en un archivo'), self._backup),
                               (_('Restaurar copia…'), _('Para un PC nuevo o tras reinstalar: no borra mods ni '
                                                         'partidas'), self._restore)):
            row = Adw.ActionRow(title=title, subtitle=sub, activatable=True)
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            row.connect("activated", lambda *_a, cb=cb: cb())
            bk.add(row)
        page.add(bk)

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

    def _backup(self):
        import time
        from pathlib import Path
        from gi.repository import Gio
        from .. import backup
        dlg = Gtk.FileDialog(title=_('Crear copia de seguridad'),
                             initial_name=f"crisol-{time.strftime('%Y-%m-%d')}.tar.gz")
        dlg.set_initial_folder(Gio.File.new_for_path(str(Path.home())))
        win = self.win

        def picked(d, res):
            try:
                path = Path(d.save_finish(res).get_path())
            except GLib.Error:
                return
            run_async(backup.create, lambda m: self.add_toast(Adw.Toast(
                title=_('Copia guardada: {0} juegos en {1}').format(len(m["games"]), path.name))),
                lambda e: self.add_toast(Adw.Toast(title=str(e))), path, self.with_saves.get_active())
        dlg.save(win, None, picked)

    def _restore(self):
        from pathlib import Path
        from gi.repository import Gio
        from .. import backup
        dlg = Gtk.FileDialog(title=_('Restaurar copia de seguridad'))
        f = Gtk.FileFilter(name=_('Copia de Crisol (.tar.gz)'))
        f.add_pattern("*.tar.gz")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(f)
        dlg.set_filters(filters)
        dlg.set_initial_folder(Gio.File.new_for_path(str(Path.home())))
        win = self.win

        def picked(d, res):
            try:
                path = Path(d.open_finish(res).get_path())
                man = backup.read_manifest(path)
            except GLib.Error:
                return
            except backup.BackupError as e:
                self.add_toast(Adw.Toast(title=str(e)))
                return
            import time
            ask = Adw.AlertDialog(heading=_('¿Restaurar la copia del {0}?').format(
                time.strftime("%d/%m/%Y %H:%M", time.localtime(man.get("created") or 0))),
                body=_('{0} juegos. Se sustituyen los ajustes y el estado de los juegos cuyos mods están en este PC; '
                       'del resto se guarda la lista para importarla desde la página del juego. Antes se hace una '
                       'copia de lo que hay ahora.').format(len(man.get("games") or [])))
            ask.add_response("cancel", _('Cancelar'))
            ask.add_response("restore", _('Restaurar'))
            ask.set_response_appearance("restore", Adw.ResponseAppearance.SUGGESTED)
            ask.connect("response", lambda _d, r: r == "restore" and self._do_restore(path))
            ask.present(self)
        dlg.open(win, None, picked)

    def _do_restore(self, path):
        from .. import backup, manager
        from ..config import Config

        def done(r):
            manager._contexts.clear()        # el estado de los juegos ha cambiado en disco
            self.ctl.cfg = Config.load()
            self.win.refresh_library()
            body = _('Juegos recuperados: {0}.').format(len(r.games))
            if r.lists:
                body += " " + _('En {0} juegos faltan los mods: abre cada uno y pulsa «Importar» en el aviso de '
                                'arriba.').format(len(r.lists))
            if r.saves:
                body += " " + _('Copias de partidas añadidas: {0}.').format(r.saves)
            d = Adw.AlertDialog(heading=_('Copia restaurada'), body=body)
            d.add_response("ok", _('Aceptar'))
            d.present(self)
        run_async(backup.restore, done, lambda e: self.add_toast(Adw.Toast(title=str(e))), path)

    def _notifications(self, row, _p):
        self.ctl.cfg.notifications = row.get_active()
        self.ctl.cfg.save()

    def _background(self, row, _p):
        from .. import notify
        try:
            notify.set_background_checks(row.get_active())
        except OSError as e:
            self.add_toast(Adw.Toast(title=str(e)))

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
