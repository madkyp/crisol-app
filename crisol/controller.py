"""Estado compartido de la interfaz: juegos detectados, enlace con Nexus y cuenta."""
from __future__ import annotations

import logging

from . import manager, secrets
from .config import Config
from .games import Game, fetch_missing_covers, scan_all
from .providers.base import ProviderError
from .providers.nexus import Nexus

log = logging.getLogger(__name__)


class Controller:
    def __init__(self):
        self.cfg = Config.load()
        self.nexus = Nexus(secrets.get_nexus_key(), self.cfg.show_adult)
        self.games: list[Game] = []
        self.account: dict | None = None      # respuesta de users/validate
        self.account_error: str = ""

    # ---------- bloqueantes (llamar desde hilos) ----------
    def scan(self, refresh: bool = True) -> list[Game]:
        games = scan_all()
        if refresh:
            fetch_missing_covers(games)
        have_table = bool(self.nexus.games(refresh))  # tabla de juegos de Nexus (caché semanal)
        for g in games:
            st = manager.context(g).state
            # None = aún sin emparejar; "" = emparejado sin resultado (o desvinculado a mano).
            if st.nexus_domain is None and have_table:
                found = None
                try:
                    found = self.nexus.find_game(g.name, g.source_id if g.source == "steam" else None)
                except ProviderError as e:
                    log.warning("%s", e)
                manual = self.cfg.nexus_domains.get(g.key)
                st.nexus_domain = manual or (found[0] if found else "")
                st.save()
        self.games = games
        return games

    def validate_account(self) -> dict | None:
        self.account, self.account_error = None, ""
        if not self.nexus.api_key:
            return None
        try:
            self.account = self.nexus.validate_key()
        except ProviderError as e:
            self.account_error = str(e)
        return self.account

    # ---------- consultas ----------
    def domain(self, game: Game) -> str:
        return manager.context(game).state.nexus_domain or ""

    def set_domain(self, game: Game, domain: str) -> None:
        st = manager.context(game).state
        st.nexus_domain = domain
        st.save()
        if domain:
            self.cfg.nexus_domains[game.key] = domain
        else:
            self.cfg.nexus_domains.pop(game.key, None)
        self.cfg.save()

    def compatible(self, game: Game) -> bool:
        return bool(self.domain(game))

    def games_for_domain(self, domain: str) -> list[Game]:
        return [g for g in self.games if self.domain(g) == domain]

    def set_api_key(self, key: str | None) -> str:
        """Guarda (o borra) la clave. Devuelve dónde quedó: "keyring", "file" o ""."""
        where = ""
        if key:
            where = secrets.set_nexus_key(key)
        else:
            secrets.clear_nexus_key()
        self.nexus.api_key = key or None
        return where

    @property
    def is_premium(self) -> bool:
        return bool(self.account and self.account.get("is_premium"))
