"""Ajustes de la aplicación (~/.config/crisol/config.json)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from . import jsonio, paths


@dataclass
class Config:
    accent: str = "#e0703a"
    show_all_games: bool = False
    show_adult: bool = False
    # Enlace juego → dominio de Nexus elegido o corregido a mano: {"steam:1771300": "kingdomcomedeliverance2"}
    nexus_domains: dict[str, str] = field(default_factory=dict)
    window_width: int = 1280
    window_height: int = 820

    @classmethod
    def load(cls) -> "Config":
        raw = jsonio.load(paths.CONFIG_FILE, {})
        known = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def save(self) -> None:
        jsonio.save(paths.CONFIG_FILE, asdict(self))
