"""Geocoding de cidade → bounding box, para montar a grade de busca.

Usa a Geocoding API, que é SKU separado da Places API: chamada aqui NÃO
consome a franquia de captação. São poucas chamadas de qualquer forma — uma
por cidade, uma única vez.
"""

from __future__ import annotations

import logging

import httpx

from leads.services.grade import BoundingBox

logger = logging.getLogger(__name__)

_ENDPOINT = "https://maps.googleapis.com/maps/api/geocode/json"


class GeocodingError(Exception):
    """Cidade não encontrada ou resposta sem bounds utilizáveis."""


async def geocodar_cidade(
    cidade: str,
    estado: str,
    api_key: str,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> BoundingBox:
    """Devolve o bbox da cidade.

    Prefere `geometry.bounds` (a extensão real do município); cai para
    `geometry.viewport` quando a API não devolve bounds, o que acontece em
    localidades pequenas.

    Raises:
        GeocodingError: sem resultado ou sem bounds/viewport na resposta.
    """
    fecha_depois = http_client is None
    client = http_client or httpx.AsyncClient(timeout=15.0)

    try:
        resposta = await client.get(
            _ENDPOINT,
            params={
                "address": f"{cidade}, {estado}, Brasil",
                "language": "pt-BR",
                "region": "br",
                "key": api_key,
            },
        )
        resposta.raise_for_status()
        dados = resposta.json()
    finally:
        if fecha_depois:
            await client.aclose()

    status = dados.get("status")
    if status != "OK" or not dados.get("results"):
        # Nunca logamos a resposta crua: ela ecoa a chave em alguns erros.
        raise GeocodingError(f"geocoding falhou para {cidade}/{estado} (status={status})")

    geometry = dados["results"][0].get("geometry", {})
    caixa = geometry.get("bounds") or geometry.get("viewport")
    if not caixa:
        raise GeocodingError(f"geocoding sem bounds para {cidade}/{estado}")

    sudoeste = caixa["southwest"]
    nordeste = caixa["northeast"]

    return BoundingBox(
        sul=sudoeste["lat"],
        oeste=sudoeste["lng"],
        norte=nordeste["lat"],
        leste=nordeste["lng"],
    )
