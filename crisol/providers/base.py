"""Interfaz común de las fuentes de mods."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from ..i18n import _


class ProviderError(Exception):
    """Error con mensaje apto para mostrar en la interfaz."""


class AuthError(ProviderError):
    pass


class RateLimited(ProviderError):
    pass


class PremiumRequired(ProviderError):
    pass


class NotFound(ProviderError):
    pass


@dataclass
class ModInfo:
    provider: str
    mod_id: int
    name: str
    author: str = ""
    version: str = ""
    summary: str = ""
    downloads: int = 0
    endorsements: int = 0
    category: str = ""
    thumbnail: str = ""
    picture: str = ""
    updated: str = ""
    adult: bool = False
    page_url: str = ""
    uid: str = ""             # id global del proveedor (para consultas en lote)
    requirements: list[dict] = field(default_factory=list)


@dataclass
class FileInfo:
    file_id: int
    name: str
    version: str
    category: str             # MAIN / OPTIONAL / UPDATE / MISCELLANEOUS / OLD_VERSION / ARCHIVED
    size: int
    file_name: str
    description: str = ""
    date: int = 0


@dataclass
class CollectionInfo:
    slug: str
    name: str
    summary: str = ""
    image: str = ""
    author: str = ""
    mod_count: int = 0
    size: int = 0
    endorsements: int = 0
    downloads: int = 0
    revision: int = 0
    adult: bool = False


@dataclass
class CollectionMod:
    mod_id: int
    file_id: int
    name: str               # nombre del mod
    file_name: str          # título del archivo
    version: str
    size: int
    optional: bool


@dataclass
class SearchPage:
    mods: list[ModInfo]
    total: int


SORTS = {  # clave interna → etiqueta
    "relevance": _('Relevancia'),
    "downloads": _('Más descargados'),
    "endorsements": _('Mejor valorados'),
    "updatedAt": _('Actualizados'),
    "createdAt": _('Más recientes'),
}


class ModProvider(ABC):
    id: str
    name: str

    @abstractmethod
    def search(self, game_domain: str, query: str, sort: str = "relevance", offset: int = 0,
               count: int = 20) -> SearchPage: ...

    @abstractmethod
    def mod(self, game_domain: str, mod_id: int) -> ModInfo: ...

    @abstractmethod
    def files(self, game_domain: str, mod_id: int) -> list[FileInfo]: ...

    @abstractmethod
    def latest_versions(self, game_domain: str, mod_ids: list[int]) -> dict[int, str]: ...
