"""Testes do corte de estabelecimentos fechados.

O caso real: um salão permanentemente fechado chegou à fila de verificação
manual. O filtro já existia — o que faltava era (a) o mesmo critério nas duas
fontes e (b) o descarte aparecer em algum contador, porque descarte silencioso
e bug de parsing são indistinguíveis quando ninguém conta.

A regressão que estes testes travam: fechado NÃO vira linha em `Prospect`.
Não basta não aparecer na fila — não pode existir no banco.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from leads.filters.site_validator import EVIDENCIA_SEM_URL, SiteVerdict
from leads.models import Nicho, Prospect, Quadrante, Segmento, Varredura
from leads.services import captacao
from leads.sources.foursquare import FoursquareSource
from leads.sources.google_places import GooglePlacesSource
from leads.sources.models import ProspectCandidate
from leads.utils.telefone import TipoTelefone

SEM_URL = SiteVerdict(tem_site_real=False, evidencia=EVIDENCIA_SEM_URL)


# --------------------------------------------------------------------------
# Camada de parsing: o que cada fonte considera "fechado"
# --------------------------------------------------------------------------


def test_foursquare_marca_inativo_quando_date_closed_vem_preenchido():
    [candidato] = FoursquareSource._parsear(
        [
            {
                "fsq_place_id": "fsq_1",
                "name": "Salão Fechado",
                "date_closed": "2024-03-11",
            }
        ]
    )

    assert not candidato.ativo
    assert candidato.fechado_evidencia == "date_closed=2024-03-11"


def test_foursquare_ativo_quando_date_closed_ausente():
    """A API OMITE o campo quando é nulo — ausência não pode virar 'fechado'.

    Verificado contra a API real: o objeto de resposta simplesmente não traz a
    chave. Se ausência fosse lida como fechamento, a captação inteira zeraria.
    """
    [candidato] = FoursquareSource._parsear(
        [{"fsq_place_id": "fsq_1", "name": "Salão Aberto"}]
    )

    assert candidato.ativo
    assert candidato.fechado_evidencia == ""


@pytest.mark.parametrize("vazio", [None, "", "   "])
def test_foursquare_date_closed_vazio_nao_e_fechamento(vazio):
    [candidato] = FoursquareSource._parsear(
        [{"fsq_place_id": "fsq_1", "name": "Salão", "date_closed": vazio}]
    )

    assert candidato.ativo


@pytest.mark.parametrize(
    "status", ["CLOSED_PERMANENTLY", "CLOSED_TEMPORARILY"]
)
def test_google_marca_inativo_nos_dois_estados_de_fechamento(status):
    """Temporário também sai, para o critério bater com o da Foursquare.

    `date_closed` não separa permanente de temporário; manter só o permanente
    aqui deixaria as duas fontes com definições diferentes de "fechado".
    """
    [candidato] = GooglePlacesSource._parsear(
        [
            {
                "id": "g_1",
                "displayName": {"text": "Salão Fechado"},
                "businessStatus": status,
            }
        ]
    )

    assert not candidato.ativo
    assert candidato.fechado_evidencia == f"businessStatus={status}"


def test_google_operational_e_ausencia_seguem_ativos():
    candidatos = GooglePlacesSource._parsear(
        [
            {
                "id": "g_1",
                "displayName": {"text": "Aberto"},
                "businessStatus": "OPERATIONAL",
            },
            {"id": "g_2", "displayName": {"text": "Sem o campo"}},
        ]
    )

    assert all(c.ativo for c in candidatos)


# --------------------------------------------------------------------------
# Camada de orquestração: fechado não vira Prospect
# --------------------------------------------------------------------------


@pytest.fixture
def quadrante(db):
    return Quadrante.objects.create(
        cidade="Maringá",
        estado="PR",
        rotulo="Q1",
        sul=-23.47,
        oeste=-52.03,
        norte=-23.41,
        leste=-51.95,
    )


def _candidato(origem_id, *, ativo=True, evidencia="", telefone=""):
    return ProspectCandidate(
        origem="FOURSQUARE",
        origem_id=origem_id,
        nome=f"Salão {origem_id}",
        endereco="Rua X, Maringá",
        telefone=telefone,
        website_url="",
        ativo=ativo,
        fechado_evidencia=evidencia,
    )


class _FonteFalsa:
    """Fonte que devolve candidatos fixos, sem rede."""

    def __init__(self, candidatos):
        self._candidatos = candidatos

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def buscar(self, texto_query, celula=None, segmento=None):
        from leads.sources.models import ResultadoBusca

        return ResultadoBusca(candidatos=self._candidatos, total_requisicoes=1)


class _ValidadorFalso:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def validar(self, url):
        return SEM_URL


def _rodar(monkeypatch, candidatos, quadrante):
    monkeypatch.setattr(captacao, "criar_fonte", lambda _f: _FonteFalsa(candidatos))
    monkeypatch.setattr(captacao, "SiteValidator", _ValidadorFalso)
    return captacao.executar_varredura(
        nicho=Nicho.objects.get(codigo="beleza"),
        segmento=Segmento.SALAO,
        consulta="Salão de beleza em Maringá PR",
        cidade="Maringá",
        estado="PR",
        quadrante=quadrante,
        fonte="foursquare",
    )


@pytest.mark.django_db
def test_fechado_nao_gera_prospect(monkeypatch, quadrante):
    """A regressão principal: fechado não existe no banco, nem na fila."""
    _rodar(
        monkeypatch,
        [_candidato("fechado", ativo=False, evidencia="date_closed=2024-03-11")],
        quadrante,
    )

    assert not Prospect.objects.filter(origem_id="fechado").exists()
    assert Prospect.objects.count() == 0


@pytest.mark.django_db
def test_fechado_e_contado_separado_e_nao_entra_em_encontrados(
    monkeypatch, quadrante
):
    """`total_fechados` é métrica própria, distinta de dedup e de banimento."""
    varredura = _rodar(
        monkeypatch,
        [
            _candidato("aberto_1"),
            _candidato("aberto_2"),
            _candidato("fechado", ativo=False, evidencia="date_closed=2024-03-11"),
        ],
        quadrante,
    )

    assert varredura.total_fechados == 1
    # `total_encontrados` conta o que sobrou DEPOIS do corte.
    assert varredura.total_encontrados == 2
    assert varredura.total_novos == 2
    assert Prospect.objects.count() == 2


@pytest.mark.django_db
def test_varredura_sem_fechados_zera_o_contador(monkeypatch, quadrante):
    varredura = _rodar(monkeypatch, [_candidato("aberto_1")], quadrante)

    assert varredura.total_fechados == 0
    assert Varredura.objects.get(pk=varredura.pk).total_fechados == 0


@pytest.mark.django_db
def test_telefone_e_normalizado_e_classificado_na_gravacao(monkeypatch, quadrante):
    _rodar(
        monkeypatch,
        [
            _candidato("cel", telefone="(44) 99912-0926"),
            _candidato("fixo", telefone="(44) 3244-6413"),
        ],
        quadrante,
    )

    celular = Prospect.objects.get(origem_id="cel")
    assert celular.telefone == "+5544999120926"
    assert celular.telefone_tipo == TipoTelefone.CELULAR
    assert celular.telefone_e_celular

    fixo = Prospect.objects.get(origem_id="fixo")
    assert fixo.telefone == "+554432446413"
    assert fixo.telefone_tipo == TipoTelefone.FIXO
    assert not fixo.telefone_e_celular


# --------------------------------------------------------------------------
# Recheca retroativa
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_consultar_fechamento_devolve_evidencia():
    respx.get("https://places-api.foursquare.com/places/fsq_1").mock(
        return_value=httpx.Response(
            200, json={"fsq_place_id": "fsq_1", "date_closed": "2024-03-11"}
        )
    )

    async with FoursquareSource("chave") as fonte:
        assert await fonte.consultar_fechamento("fsq_1") == "date_closed=2024-03-11"


@pytest.mark.asyncio
@respx.mock
async def test_consultar_fechamento_vazio_quando_a_fonte_nao_afirma_nada():
    """Vazio = 'a fonte não disse que fechou', jamais 'confirmado aberto'."""
    respx.get("https://places-api.foursquare.com/places/fsq_1").mock(
        return_value=httpx.Response(200, json={"fsq_place_id": "fsq_1"})
    )

    async with FoursquareSource("chave") as fonte:
        assert await fonte.consultar_fechamento("fsq_1") == ""
