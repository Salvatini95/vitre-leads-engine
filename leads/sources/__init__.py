"""Registro das fontes de captação.

Único lugar que sabe quais fontes existem. O comando `captar` monta o
`choices` do `--fonte` a partir daqui, e a orquestração instancia por nome —
nenhum dos dois importa uma fonte concreta.

Acrescentar fonte = implementar `FonteDeProspects` e somar a classe a
`_CLASSES`. Nada mais muda.
"""

from __future__ import annotations

from django.conf import settings

from leads.sources.base import FonteDeProspects
from leads.sources.foursquare import FoursquareSource
from leads.sources.google_places import GooglePlacesSource

# Google Places continua sendo o padrão: é a fonte com cobertura comprovada
# para pequeno negócio em Maringá. A Foursquare é contingência enquanto o
# billing do Google está bloqueado.
FONTE_PADRAO = GooglePlacesSource.NOME

_CLASSES: tuple[type[FonteDeProspects], ...] = (GooglePlacesSource, FoursquareSource)

FONTES: dict[str, type[FonteDeProspects]] = {
    classe.NOME: classe for classe in _CLASSES
}

# Cada fonte lê sua própria chave do settings. O valor nunca é logado.
_SETTING_CHAVE: dict[str, str] = {
    GooglePlacesSource.NOME: "GOOGLE_PLACES_API_KEY",
    FoursquareSource.NOME: "FOURSQUARE_API_KEY",
}


def classe_da_fonte(nome: str) -> type[FonteDeProspects]:
    """Resolve o nome do `--fonte` para a classe. Não instancia."""
    try:
        return FONTES[nome]
    except KeyError:
        disponiveis = ", ".join(sorted(FONTES))
        raise ValueError(
            f"Fonte desconhecida: {nome!r}. Disponíveis: {disponiveis}"
        ) from None


def criar_fonte(nome: str) -> FonteDeProspects:
    """Instancia a fonte com a chave de API correspondente.

    Raises:
        ValueError: fonte desconhecida, ou chave de API ausente no `.env`
            (cada fonte valida a própria chave no construtor).
    """
    classe = classe_da_fonte(nome)
    api_key = getattr(settings, _SETTING_CHAVE[nome], "")

    if classe is FoursquareSource:
        return FoursquareSource(
            api_key,
            api_base=settings.FOURSQUARE_API_BASE,
            campos_pro=settings.FOURSQUARE_CAMPOS_PRO,
        )

    return classe(api_key)


__all__ = [
    "FONTES",
    "FONTE_PADRAO",
    "FonteDeProspects",
    "FoursquareSource",
    "GooglePlacesSource",
    "classe_da_fonte",
    "criar_fonte",
]
