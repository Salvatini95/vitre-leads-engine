"""Testes do cliente Foursquare.

Os dois casos que mais importam aqui são silenciosos:

- A nota vem em escala 0–10 e o contrato é 0–5. Sem normalizar, todo prospect
  da Foursquare sobe ao topo da fila de curadoria do Admin, que ordena por
  nota. Nada quebra — a fila só fica errada.
- A paginação é por cursor no header `Link`. Se o header for ignorado, a busca
  para na primeira página e o quadrante parece ter 50 estabelecimentos.

Nenhum teste faz chamada real: tudo passa por respx.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from leads.services.grade import CelulaGrade
from leads.sources.foursquare import (
    _CATEGORIAS_POR_SEGMENTO,
    _FIELDS_BASE,
    _FIELDS_LEGADOS,
    _FIELDS_PRO,
    FoursquareSource,
)

CELULA = CelulaGrade(rotulo="Q1", sul=-23.47, oeste=-52.03, norte=-23.41, leste=-51.95)

BASE_LEGADA = "https://api.foursquare.com/v3"


def _endpoint(fonte_base: str | None = None) -> str:
    base = fonte_base or "https://places-api.foursquare.com"
    return f"{base}/places/search"


def _pagina(qtd: int, *, inicio: int = 0) -> dict:
    return {
        "results": [
            {
                "fsq_place_id": f"fsq_{inicio + n}",
                "name": f"Salão {inicio + n}",
                "location": {"formatted_address": "Rua X, Maringá, PR"},
                "tel": "(44) 3333-3333",
                "website": "",
                "categories": [{"name": "Health and Beauty Service"}],
                "rating": 8.0,
                "stats": {"total_ratings": 120},
            }
            for n in range(qtd)
        ]
    }


def _com_cursor(cursor: str) -> dict[str, str]:
    return {"Link": f'<{_endpoint()}?cursor={cursor}>; rel="next"'}


@pytest.mark.asyncio
async def test_envia_chave_e_versao_no_header():
    with respx.mock:
        rota = respx.get(_endpoint()).mock(
            return_value=httpx.Response(200, json=_pagina(2))
        )
        async with FoursquareSource("chave-teste") as fonte:
            await fonte.buscar("salão de beleza em Maringá PR", CELULA, "SALAO")

    req = rota.calls[0].request
    assert req.headers["Authorization"] == "Bearer chave-teste"
    assert req.headers["X-Places-Api-Version"]


@pytest.mark.asyncio
async def test_base_v3_legada_usa_auth_sem_bearer():
    """Chave v3 antiga não fala Bearer — mandar Bearer nela é 401."""
    with respx.mock:
        rota = respx.get(_endpoint(BASE_LEGADA)).mock(
            return_value=httpx.Response(200, json=_pagina(1))
        )
        async with FoursquareSource("chave-v3", api_base=BASE_LEGADA) as fonte:
            await fonte.buscar("salão", CELULA, "SALAO")

    req = rota.calls[0].request
    assert req.headers["Authorization"] == "chave-v3"
    assert "X-Places-Api-Version" not in req.headers


@pytest.mark.asyncio
async def test_envia_cantos_do_quadrante():
    """ne/sw é o recorte que dá a cada quadrante seu próprio teto."""
    with respx.mock:
        rota = respx.get(_endpoint()).mock(
            return_value=httpx.Response(200, json=_pagina(1))
        )
        async with FoursquareSource("k") as fonte:
            await fonte.buscar("nail designer em Maringá PR", CELULA, "NAIL")

    params = rota.calls[0].request.url.params
    assert params["ne"] == f"{CELULA.norte},{CELULA.leste}"
    assert params["sw"] == f"{CELULA.sul},{CELULA.oeste}"
    # ll/radius junto com ne/sw é 400 na API.
    assert "ll" not in params
    assert "radius" not in params


@pytest.mark.asyncio
async def test_busca_sem_celula_nao_manda_recorte():
    with respx.mock:
        rota = respx.get(_endpoint()).mock(
            return_value=httpx.Response(200, json=_pagina(1))
        )
        async with FoursquareSource("k") as fonte:
            await fonte.buscar("salão em Maringá PR", None, "SALAO")

    params = rota.calls[0].request.url.params
    assert "ne" not in params
    assert "sw" not in params


@pytest.mark.asyncio
async def test_busca_por_categoria_e_nao_por_texto():
    """A regra que separa salão de hotel.

    Medido em Maringá: com `query` em português a API devolve hotel,
    universidade e supermercado; com `fsq_category_ids` devolve salão. Se
    alguém trocar isto de volta por busca textual, a captação continua
    "funcionando" e o resultado vira lixo.
    """
    with respx.mock:
        rota = respx.get(_endpoint()).mock(
            return_value=httpx.Response(200, json=_pagina(1))
        )
        async with FoursquareSource("k") as fonte:
            await fonte.buscar("salão de beleza em Maringá PR", CELULA, "SALAO")

    params = rota.calls[0].request.url.params
    assert "query" not in params
    assert set(params["fsq_category_ids"].split(",")) == set(
        _CATEGORIAS_POR_SEGMENTO["SALAO"]
    )


@pytest.mark.asyncio
async def test_segmento_sem_categoria_cai_no_texto():
    """OUTRO não tem categoria — melhor texto ruidoso que busca vazia."""
    with respx.mock:
        rota = respx.get(_endpoint()).mock(
            return_value=httpx.Response(200, json=_pagina(1))
        )
        async with FoursquareSource("k") as fonte:
            await fonte.buscar("tatuagem em Maringá PR", CELULA, "OUTRO")

    params = rota.calls[0].request.url.params
    assert params["query"] == "tatuagem em Maringá PR"
    assert "fsq_category_ids" not in params


def test_todo_segmento_operacional_tem_categoria():
    """Segmento novo sem categoria mapeada degrada para busca textual — que
    nesta API é ruído. O teste é o lembrete."""
    from leads.models import Segmento

    esperados = {s.value for s in Segmento} - {Segmento.OUTRO.value}
    assert esperados <= set(_CATEGORIAS_POR_SEGMENTO)


def test_pede_os_campos_do_dominio():
    """Campo não pedido não vem na resposta — diferente do Google, aqui o
    field mask não muda preço, muda o que existe no JSON."""
    assert {
        "name",
        "location",
        "latitude",
        "longitude",
        "website",
        "categories",
    } <= set(_FIELDS_BASE)


@pytest.mark.asyncio
async def test_por_padrao_nao_pede_campo_pago():
    """`rating`/`stats` consomem crédito de API. Sem crédito a resposta é 429
    e a busca INTEIRA morre — não só a nota. Padrão tem de ser gratuito."""
    with respx.mock:
        rota = respx.get(_endpoint()).mock(
            return_value=httpx.Response(200, json=_pagina(1))
        )
        async with FoursquareSource("k") as fonte:
            await fonte.buscar("salão", CELULA, "SALAO")

    campos = set(rota.calls[0].request.url.params["fields"].split(","))
    assert campos.isdisjoint(_FIELDS_PRO)
    assert "website" in campos


@pytest.mark.asyncio
async def test_campos_pro_sao_opt_in():
    with respx.mock:
        rota = respx.get(_endpoint()).mock(
            return_value=httpx.Response(200, json=_pagina(1))
        )
        async with FoursquareSource("k", campos_pro=True) as fonte:
            await fonte.buscar("salão", CELULA, "SALAO")

    campos = set(rota.calls[0].request.url.params["fields"].split(","))
    assert set(_FIELDS_PRO) <= campos


@pytest.mark.asyncio
async def test_normaliza_rating_de_dez_para_cinco():
    """A Foursquare pontua 0–10; o contrato é 0–5, a escala do Google."""
    with respx.mock:
        respx.get(_endpoint()).mock(return_value=httpx.Response(200, json=_pagina(1)))
        async with FoursquareSource("k", campos_pro=True) as fonte:
            resultado = await fonte.buscar("salão", CELULA, "SALAO")

    assert resultado.candidatos[0].rating == 4.0
    assert resultado.candidatos[0].total_avaliacoes == 120


@pytest.mark.asyncio
async def test_sem_rating_nao_inventa_nota():
    corpo = _pagina(1)
    del corpo["results"][0]["rating"]
    with respx.mock:
        respx.get(_endpoint()).mock(return_value=httpx.Response(200, json=corpo))
        async with FoursquareSource("k") as fonte:
            resultado = await fonte.buscar("salão", CELULA, "SALAO")

    assert resultado.candidatos[0].rating is None


@pytest.mark.asyncio
async def test_mapeia_campos_para_o_contrato_neutro():
    with respx.mock:
        respx.get(_endpoint()).mock(return_value=httpx.Response(200, json=_pagina(1)))
        async with FoursquareSource("k") as fonte:
            resultado = await fonte.buscar("salão", CELULA, "SALAO")

    candidato = resultado.candidatos[0]
    assert candidato.origem == "FOURSQUARE"
    assert candidato.origem_id == "fsq_0"
    assert candidato.nome == "Salão 0"
    assert candidato.endereco == "Rua X, Maringá, PR"
    assert candidato.telefone == "(44) 3333-3333"
    assert candidato.website_url == ""
    assert candidato.categoria == "Health and Beauty Service"
    assert candidato.ativo is True


@pytest.mark.asyncio
async def test_mapeia_localizacao_estruturada_e_coordenadas_atuais():
    corpo = _pagina(1)
    corpo["results"][0].update(
        {
            "location": {
                "address": "Rua das Flores, 123",
                "locality": "Curitiba",
                "region": "PR",
                "country": "br",
                "postcode": "80000-000",
            },
            "latitude": -25.4284,
            "longitude": -49.2733,
        }
    )

    with respx.mock:
        respx.get(_endpoint()).mock(return_value=httpx.Response(200, json=corpo))
        async with FoursquareSource("k") as fonte:
            resultado = await fonte.buscar("salão", CELULA, "SALAO")

    candidato = resultado.candidatos[0]
    assert candidato.endereco == "Rua das Flores, 123"
    assert candidato.bairro is None
    assert candidato.cidade == "Curitiba"
    assert candidato.estado == "PR"
    assert candidato.pais == "BR"
    assert candidato.cep == "80000-000"
    assert candidato.latitude == -25.4284
    assert candidato.longitude == -49.2733


@pytest.mark.asyncio
async def test_aceita_fallback_legado_sem_inferir_cidade_ou_estado():
    corpo = _pagina(1)
    corpo["results"][0]["location"] = {
        "formatted_address": "Rua X, Maringá, PR"
    }
    corpo["results"][0]["geocodes"] = {
        "main": {"latitude": -23.4205, "longitude": -51.9333}
    }

    with respx.mock:
        rota = respx.get(_endpoint(BASE_LEGADA)).mock(
            return_value=httpx.Response(200, json=corpo)
        )
        async with FoursquareSource("k", api_base=BASE_LEGADA) as fonte:
            resultado = await fonte.buscar("salão", CELULA, "SALAO")

    campos = set(rota.calls[0].request.url.params["fields"].split(","))
    candidato = resultado.candidatos[0]
    assert set(_FIELDS_LEGADOS) <= campos
    assert "latitude" not in campos
    assert "longitude" not in campos
    assert candidato.endereco == "Rua X, Maringá, PR"
    assert candidato.cidade is None
    assert candidato.estado is None
    assert candidato.latitude == -23.4205
    assert candidato.longitude == -51.9333


def test_localizacao_parcial_ou_ausente_permanece_desconhecida():
    parcial = {
        "fsq_place_id": "parcial",
        "name": "Salão Parcial",
        "location": {"locality": "Maringá"},
    }
    ausente = {"fsq_place_id": "ausente", "name": "Salão Ausente"}

    candidato_parcial, candidato_ausente = FoursquareSource._parsear(
        [parcial, ausente]
    )

    assert candidato_parcial.cidade == "Maringá"
    assert candidato_parcial.endereco is None
    assert candidato_parcial.estado is None
    assert candidato_parcial.latitude is None
    assert candidato_ausente.endereco is None
    assert candidato_ausente.cidade is None
    assert candidato_ausente.longitude is None


@pytest.mark.parametrize(
    "latitude,longitude",
    [(-23.42, None), (None, -51.93), (91.0, -51.93), (-23.42, float("inf"))],
)
def test_coordenadas_parciais_ou_invalidas_nao_formam_localizacao(
    latitude, longitude
):
    lugar = {
        "fsq_place_id": "coordenada",
        "name": "Salão Coordenada",
        "latitude": latitude,
        "longitude": longitude,
    }

    candidato = FoursquareSource._parsear([lugar])[0]

    assert candidato.latitude is None
    assert candidato.longitude is None


@pytest.mark.asyncio
async def test_aceita_fsq_id_da_v3_legada():
    """A v3 chama o id de `fsq_id`; a geração atual, de `fsq_place_id`."""
    corpo = _pagina(1)
    corpo["results"][0] = {
        k: v for k, v in corpo["results"][0].items() if k != "fsq_place_id"
    }
    corpo["results"][0]["fsq_id"] = "id_legado"

    with respx.mock:
        respx.get(_endpoint(BASE_LEGADA)).mock(
            return_value=httpx.Response(200, json=corpo)
        )
        async with FoursquareSource("k", api_base=BASE_LEGADA) as fonte:
            resultado = await fonte.buscar("salão", CELULA, "SALAO")

    assert resultado.candidatos[0].origem_id == "id_legado"


@pytest.mark.asyncio
async def test_pagina_pelo_cursor_do_header_link():
    with respx.mock:
        rota = respx.get(_endpoint()).mock(
            side_effect=[
                httpx.Response(200, json=_pagina(50), headers=_com_cursor("c1")),
                httpx.Response(200, json=_pagina(50, inicio=50)),
            ]
        )
        async with FoursquareSource("k") as fonte:
            resultado = await fonte.buscar("salão", CELULA, "SALAO")

    assert resultado.total_requisicoes == 2
    assert len(resultado.candidatos) == 100
    assert rota.calls[1].request.url.params["cursor"] == "c1"


@pytest.mark.asyncio
async def test_pagina_ate_o_teto_de_tres_paginas():
    """Cursor infinito não pode virar varredura infinita."""
    with respx.mock:
        respx.get(_endpoint()).mock(
            side_effect=[
                httpx.Response(200, json=_pagina(50, inicio=n * 50),
                               headers=_com_cursor(f"c{n}"))
                for n in range(5)
            ]
        )
        async with FoursquareSource("k") as fonte:
            resultado = await fonte.buscar("salão", CELULA, "SALAO")

    assert resultado.total_requisicoes == 3
    assert len(resultado.candidatos) == 150


@pytest.mark.asyncio
async def test_para_quando_nao_ha_header_link():
    """Ausência de `Link` é fim de paginação legítimo, não erro."""
    with respx.mock:
        respx.get(_endpoint()).mock(
            side_effect=[
                httpx.Response(200, json=_pagina(50), headers=_com_cursor("c1")),
                httpx.Response(200, json=_pagina(7, inicio=50)),
            ]
        )
        async with FoursquareSource("k") as fonte:
            resultado = await fonte.buscar("salão", CELULA, "SALAO")

    assert resultado.total_requisicoes == 2
    assert len(resultado.candidatos) == 57


@pytest.mark.asyncio
async def test_estabelecimento_fechado_vem_marcado_inativo():
    corpo = _pagina(1)
    corpo["results"][0]["date_closed"] = "2024-03-11"
    with respx.mock:
        respx.get(_endpoint()).mock(return_value=httpx.Response(200, json=corpo))
        async with FoursquareSource("k") as fonte:
            resultado = await fonte.buscar("salão", CELULA, "SALAO")

    assert resultado.candidatos[0].ativo is False


@pytest.mark.asyncio
async def test_resultado_sem_id_ou_sem_nome_e_descartado():
    """Sem id não há dedup possível; sem nome não há o que prospectar."""
    corpo = _pagina(3)
    del corpo["results"][0]["fsq_place_id"]
    corpo["results"][1]["name"] = ""

    with respx.mock:
        respx.get(_endpoint()).mock(return_value=httpx.Response(200, json=corpo))
        async with FoursquareSource("k") as fonte:
            resultado = await fonte.buscar("salão", CELULA, "SALAO")

    assert len(resultado.candidatos) == 1
    assert resultado.candidatos[0].origem_id == "fsq_2"


@pytest.mark.asyncio
async def test_erro_carrega_requisicoes_ja_consumidas():
    """Requisição gasta conta na franquia mesmo se a página seguinte falhar."""
    from leads.sources.models import BuscaParcialError

    with respx.mock:
        respx.get(_endpoint()).mock(
            side_effect=[
                httpx.Response(200, json=_pagina(50), headers=_com_cursor("c1")),
                httpx.Response(500),
                httpx.Response(500),
                httpx.Response(500),
                httpx.Response(500),
            ]
        )
        async with FoursquareSource("k") as fonte:
            with pytest.raises(BuscaParcialError) as info:
                await fonte.buscar("salão", CELULA, "SALAO")

    assert info.value.total_requisicoes == 1
    assert len(info.value.candidatos) == 50


@pytest.mark.asyncio
async def test_retry_em_erro_5xx_transitorio():
    with respx.mock:
        respx.get(_endpoint()).mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(200, json=_pagina(5)),
            ]
        )
        async with FoursquareSource("k") as fonte:
            resultado = await fonte.buscar("salão", CELULA, "SALAO")

    assert len(resultado.candidatos) == 5


@pytest.mark.asyncio
async def test_erro_4xx_nao_e_retentado():
    """401/400 é erro de configuração: insistir só queima franquia."""
    from leads.sources.models import BuscaParcialError

    with respx.mock:
        rota = respx.get(_endpoint()).mock(return_value=httpx.Response(401))
        async with FoursquareSource("k") as fonte:
            with pytest.raises(BuscaParcialError):
                await fonte.buscar("salão", CELULA, "SALAO")

    assert rota.call_count == 1


def test_sem_api_key_falha_cedo():
    with pytest.raises(ValueError, match="FOURSQUARE_API_KEY ausente"):
        FoursquareSource("")
