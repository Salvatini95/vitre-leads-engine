"""Testes do cliente Places.

O caso mais importante aqui é o da paginação: se `nextPageToken` sumir do
field mask, a busca para na primeira página SEM erro nenhum e a cidade parece
ter só 20 estabelecimentos. É falha silenciosa, e só teste pega.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from leads.services.grade import CelulaGrade
from leads.sources import google_places
from leads.sources.google_places import _FIELD_MASK, GooglePlacesSource
from leads.sources.models import BuscaParcialError

CELULA = CelulaGrade(rotulo="Q1", sul=-23.47, oeste=-52.03, norte=-23.41, leste=-51.95)


def _pagina(qtd: int, *, token: str | None = None, inicio: int = 0) -> dict:
    corpo: dict = {
        "places": [
            {
                "id": f"place_{inicio + n}",
                "displayName": {"text": f"Salão {inicio + n}"},
                "formattedAddress": "Rua X, Maringá",
                "nationalPhoneNumber": "(44) 3333-3333",
                "websiteUri": "",
                "rating": 4.5,
                "userRatingCount": 120,
                "businessStatus": "OPERATIONAL",
            }
            for n in range(qtd)
        ]
    }
    if token:
        corpo["nextPageToken"] = token
    return corpo


@pytest.fixture(autouse=True)
def _sem_espera(monkeypatch):
    """Zera a espera de propagação do pageToken para o teste não arrastar."""
    monkeypatch.setattr(google_places, "_TOKEN_RETRY_ESPERA", 0)


def test_field_mask_declara_next_page_token():
    """Sem isto no mask a API omite o token e a paginação morre em silêncio."""
    assert "nextPageToken" in _FIELD_MASK.split(",")


def test_field_mask_nao_pede_campo_alem_do_necessario():
    """Campo a mais no mask = SKU mais caro. A lista é fechada de propósito."""
    assert set(_FIELD_MASK.split(",")) == {
        "nextPageToken",
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.nationalPhoneNumber",
        "places.websiteUri",
        "places.rating",
        "places.userRatingCount",
        "places.businessStatus",
    }


@pytest.mark.asyncio
async def test_envia_api_key_e_mask_no_header():
    with respx.mock:
        rota = respx.post(google_places._ENDPOINT).mock(
            return_value=httpx.Response(200, json=_pagina(2))
        )
        async with GooglePlacesSource("chave-teste") as fonte:
            await fonte.buscar("salão de beleza em Maringá PR", CELULA)

    req = rota.calls[0].request
    assert req.headers["X-Goog-Api-Key"] == "chave-teste"
    assert req.headers["X-Goog-FieldMask"] == _FIELD_MASK


@pytest.mark.asyncio
async def test_envia_retangulo_do_quadrante():
    """O recorte geográfico é o que permite passar do teto de 60 por cidade."""
    with respx.mock:
        rota = respx.post(google_places._ENDPOINT).mock(
            return_value=httpx.Response(200, json=_pagina(1))
        )
        async with GooglePlacesSource("k") as fonte:
            await fonte.buscar("nail designer em Maringá PR", CELULA)

    corpo = rota.calls[0].request.content.decode()
    assert '"rectangle"' in corpo
    assert str(CELULA.sul) in corpo
    assert str(CELULA.leste) in corpo


@pytest.mark.asyncio
async def test_pagina_ate_o_teto_de_tres_paginas():
    with respx.mock:
        respx.post(google_places._ENDPOINT).mock(
            side_effect=[
                httpx.Response(200, json=_pagina(20, token="t1", inicio=0)),
                httpx.Response(200, json=_pagina(20, token="t2", inicio=20)),
                httpx.Response(200, json=_pagina(20, token="t3", inicio=40)),
                httpx.Response(200, json=_pagina(20, token="t4", inicio=60)),
            ]
        )
        async with GooglePlacesSource("k") as fonte:
            resultado = await fonte.buscar("salão", CELULA)

    assert resultado.total_requisicoes == 3
    assert len(resultado.candidatos) == 60


@pytest.mark.asyncio
async def test_para_quando_nao_ha_mais_token():
    with respx.mock:
        respx.post(google_places._ENDPOINT).mock(
            side_effect=[
                httpx.Response(200, json=_pagina(20, token="t1")),
                httpx.Response(200, json=_pagina(7, inicio=20)),
            ]
        )
        async with GooglePlacesSource("k") as fonte:
            resultado = await fonte.buscar("salão", CELULA)

    assert resultado.total_requisicoes == 2
    assert len(resultado.candidatos) == 27


@pytest.mark.asyncio
async def test_estabelecimento_fechado_vem_marcado_inativo():
    corpo = _pagina(1)
    corpo["places"][0]["businessStatus"] = "CLOSED_PERMANENTLY"
    with respx.mock:
        respx.post(google_places._ENDPOINT).mock(return_value=httpx.Response(200, json=corpo))
        async with GooglePlacesSource("k") as fonte:
            resultado = await fonte.buscar("salão", CELULA)

    assert resultado.candidatos[0].ativo is False


@pytest.mark.asyncio
async def test_erro_carrega_requisicoes_ja_consumidas():
    """Requisição gasta conta na franquia mesmo se a página seguinte falhar."""
    with respx.mock:
        respx.post(google_places._ENDPOINT).mock(
            side_effect=[
                httpx.Response(200, json=_pagina(20, token="t1")),
                httpx.Response(500),
                httpx.Response(500),
                httpx.Response(500),
                httpx.Response(500),
            ]
        )
        async with GooglePlacesSource("k") as fonte:
            with pytest.raises(BuscaParcialError) as info:
                await fonte.buscar("salão", CELULA)

    assert info.value.total_requisicoes == 1
    assert len(info.value.candidatos) == 20


@pytest.mark.asyncio
async def test_retry_em_erro_5xx_transitorio():
    with respx.mock:
        respx.post(google_places._ENDPOINT).mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(200, json=_pagina(5)),
            ]
        )
        async with GooglePlacesSource("k") as fonte:
            resultado = await fonte.buscar("salão", CELULA)

    assert len(resultado.candidatos) == 5


def test_sem_api_key_falha_cedo():
    with pytest.raises(ValueError, match="GOOGLE_PLACES_API_KEY ausente"):
        GooglePlacesSource("")
