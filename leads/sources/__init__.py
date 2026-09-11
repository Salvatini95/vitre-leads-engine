"""Registro das fontes de captação.

Único lugar que sabe quais fontes existem. O comando `captar` monta o
`choices` do `--fonte` a partir daqui, e a orquestração instancia por nome —
nenhum dos dois importa uma fonte concreta.

Acrescentar fonte = implementar `FonteDeProspects`, somar a classe a `_CLASSES`
e declarar sua configuração local obrigatória neste registro.
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

# Configuração local mínima para instanciar cada fonte. O primeiro item é
# sempre a credencial usada pela fábrica; os demais são requisitos específicos.
_CONFIGURACAO_OBRIGATORIA: dict[str, tuple[str, ...]] = {
    GooglePlacesSource.NOME: ("GOOGLE_PLACES_API_KEY",),
    FoursquareSource.NOME: ("FOURSQUARE_API_KEY", "FOURSQUARE_API_BASE"),
}


class ConfiguracaoFonteError(ValueError):
    """Configuração local obrigatória de uma fonte está ausente."""


def classe_da_fonte(nome: str) -> type[FonteDeProspects]:
    """Resolve o nome do `--fonte` para a classe. Não instancia."""
    try:
        return FONTES[nome]
    except KeyError:
        disponiveis = ", ".join(sorted(FONTES))
        raise ValueError(
            f"Fonte desconhecida: {nome!r}. Disponíveis: {disponiveis}"
        ) from None


def validar_configuracao_fonte(nome: str) -> None:
    """Valida settings locais sem instanciar cliente ou realizar HTTP."""
    classe_da_fonte(nome)
    for setting_nome in _CONFIGURACAO_OBRIGATORIA[nome]:
        valor = getattr(settings, setting_nome, None)
        if valor is None or (isinstance(valor, str) and not valor.strip()):
            raise ConfiguracaoFonteError(
                f"{setting_nome} ausente — configure antes de executar a captação"
            )


def criar_fonte(nome: str) -> FonteDeProspects:
    """Instancia a fonte com a chave de API correspondente.

    Raises:
        ValueError: fonte desconhecida ou configuração local obrigatória
            ausente. Cada fonte também valida sua chave no construtor.
    """
    validar_configuracao_fonte(nome)
    classe = classe_da_fonte(nome)
    api_key = getattr(settings, _CONFIGURACAO_OBRIGATORIA[nome][0])

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
    "ConfiguracaoFonteError",
    "classe_da_fonte",
    "criar_fonte",
    "validar_configuracao_fonte",
]
