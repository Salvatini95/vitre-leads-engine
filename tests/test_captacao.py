"""Testes da orquestração da varredura.

Cobrem as regras que protegem dinheiro e trabalho manual: a trava da
franquia, o dedup na recaptura, o respeito à curadoria já feita e ao
banimento.
"""

from __future__ import annotations

import pytest

from leads.filters.site_validator import SiteVerdict
from leads.models import (
    Descarte,
    MotivoDescarte,
    Prospect,
    Quadrante,
    Segmento,
    StatusFunil,
    StatusVarredura,
    Varredura,
)
from leads.services import captacao
from leads.sources.models import ProspectCandidate

pytestmark = pytest.mark.django_db


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


def _fake_coletar(candidatos, vereditos, requisicoes=1, fechados=0):
    """Substitui a parte de rede por um retorno fixo."""

    async def _coletar(texto_query, celula, fonte=None, segmento=None):
        return captacao.Coleta(
            candidatos=candidatos,
            requisicoes=requisicoes,
            vereditos=vereditos,
            fechados=fechados,
        )

    return _coletar


SEM_SITE = SiteVerdict(tem_site_real=False, evidencia="só perfil/plataforma de terceiro")
COM_SITE = SiteVerdict(tem_site_real=True, evidencia="site ativo")


def _candidato(origem_id="place_1", nome="Salão Bella", **extra):
    return ProspectCandidate(
        origem="GOOGLE_PLACES",
        origem_id=origem_id,
        nome=nome,
        endereco="Rua X, Maringá",
        telefone="(44) 3333-3333",
        website_url=extra.pop("website_url", "https://instagram.com/salaobella"),
        rating=4.7,
        total_avaliacoes=210,
        **extra,
    )


def _rodar(monkeypatch, candidatos, vereditos, quadrante, requisicoes=1, **extra):
    monkeypatch.setattr(
        captacao, "_coletar", _fake_coletar(candidatos, vereditos, requisicoes)
    )
    return captacao.executar_varredura(
        segmento=Segmento.SALAO,
        segmento_rotulo=Segmento.SALAO.label,
        cidade="Maringá",
        estado="PR",
        quadrante=quadrante,
        **extra,
    )


def test_persiste_apenas_quem_nao_tem_site(monkeypatch, quadrante):
    candidatos = [_candidato("p1"), _candidato("p2", website_url="https://salao.com.br")]
    varredura = _rodar(monkeypatch, candidatos, [SEM_SITE, COM_SITE], quadrante)

    assert varredura.status == StatusVarredura.CONCLUIDA
    assert varredura.total_encontrados == 2
    assert varredura.total_sem_site == 1
    assert varredura.total_novos == 1
    assert Prospect.objects.count() == 1
    assert Prospect.objects.get().origem_id == "p1"


def test_prospect_nasce_em_curadoria_fora_do_funil(monkeypatch, quadrante):
    _rodar(monkeypatch, [_candidato()], [SEM_SITE], quadrante)

    prospect = Prospect.objects.get()
    assert prospect.status_funil == StatusFunil.NOVO
    assert prospect.ativo_no_funil is False
    assert prospect.e_alvo is True
    assert prospect.site_evidencia == "só perfil/plataforma de terceiro"


def test_recaptura_nao_duplica(monkeypatch, quadrante):
    _rodar(monkeypatch, [_candidato("p1")], [SEM_SITE], quadrante)
    _rodar(monkeypatch, [_candidato("p1")], [SEM_SITE], quadrante)

    assert Prospect.objects.count() == 1
    assert Varredura.objects.count() == 2


def test_recaptura_atualiza_fato_do_mundo(monkeypatch, quadrante):
    _rodar(monkeypatch, [_candidato("p1")], [SEM_SITE], quadrante)

    novo = ProspectCandidate(
        origem="GOOGLE_PLACES",
        origem_id="p1",
        nome="Salão Bella",
        telefone="(44) 99912-0926",
        website_url="https://salaobella.com.br",
        rating=4.9,
        total_avaliacoes=300,
    )
    _rodar(monkeypatch, [novo], [SEM_SITE], quadrante)

    prospect = Prospect.objects.get()
    # Gravado normalizado, não como veio da fonte.
    assert prospect.telefone == "+5544999120926"
    assert prospect.total_avaliacoes == 300


def test_recaptura_nao_desfaz_curadoria(monkeypatch, quadrante):
    """Segmento corrigido à mão não pode voltar ao que a busca disse."""
    _rodar(monkeypatch, [_candidato("p1")], [SEM_SITE], quadrante)

    prospect = Prospect.objects.get()
    prospect.segmento = Segmento.NAIL
    prospect.status_funil = StatusFunil.EM_ANDAMENTO
    prospect.revisado_manualmente = True
    prospect.save()

    _rodar(monkeypatch, [_candidato("p1")], [SEM_SITE], quadrante)

    prospect.refresh_from_db()
    assert prospect.segmento == Segmento.NAIL
    assert prospect.status_funil == StatusFunil.EM_ANDAMENTO


def test_banido_nao_volta_ao_funil(monkeypatch, quadrante):
    _rodar(monkeypatch, [_candidato("p1")], [SEM_SITE], quadrante)

    prospect = Prospect.objects.get()
    Descarte.objects.create(
        prospect=prospect,
        motivo=MotivoDescarte.FORA_DO_PERFIL,
        banido=True,
    )
    prospect.delete()

    varredura = _rodar(monkeypatch, [_candidato("p1")], [SEM_SITE], quadrante)

    assert varredura.total_sem_site == 1
    assert varredura.total_novos == 0
    assert Prospect.objects.count() == 0


def test_franquia_esgotada_bloqueia(monkeypatch, quadrante, settings):
    settings.PLACES_FRANQUIA_MENSAL = 2
    Varredura.objects.create(
        termo_busca="x",
        segmento=Segmento.SALAO,
        cidade="Maringá",
        estado="PR",
        total_requisicoes=2,
    )

    with pytest.raises(captacao.FranquiaEsgotadaError, match="Franquia mensal esgotada"):
        _rodar(monkeypatch, [_candidato()], [SEM_SITE], quadrante)


def test_saldo_desconta_o_consumo(quadrante, settings):
    settings.PLACES_FRANQUIA_MENSAL = 100
    Varredura.objects.create(
        termo_busca="x",
        segmento=Segmento.SALAO,
        cidade="Maringá",
        estado="PR",
        total_requisicoes=30,
    )
    assert captacao.consumo_do_mes() == 30
    assert captacao.saldo_da_franquia() == 70


def test_fonte_padrao_continua_sendo_google(monkeypatch, quadrante):
    """Sem `--fonte`, nada muda: Google Places segue primária."""
    varredura = _rodar(monkeypatch, [_candidato()], [SEM_SITE], quadrante)
    assert varredura.fonte == "GOOGLE_PLACES"


def test_varredura_grava_a_fonte_usada(monkeypatch, quadrante):
    varredura = _rodar(
        monkeypatch, [_candidato()], [SEM_SITE], quadrante, fonte="foursquare"
    )
    assert varredura.fonte == "FOURSQUARE"


def test_franquia_e_contada_por_fonte(monkeypatch, quadrante, settings):
    """Captação na Foursquare não pode descontar da franquia do Google."""
    settings.PLACES_FRANQUIA_MENSAL = 2
    settings.FOURSQUARE_FRANQUIA_MENSAL = 100

    Varredura.objects.create(
        termo_busca="x",
        segmento=Segmento.SALAO,
        fonte="GOOGLE_PLACES",
        cidade="Maringá",
        estado="PR",
        total_requisicoes=2,
    )

    assert captacao.saldo_da_franquia("google_places") == 0
    assert captacao.saldo_da_franquia("foursquare") == 100

    # Google travado, Foursquare passa.
    with pytest.raises(captacao.FranquiaEsgotadaError):
        _rodar(monkeypatch, [_candidato()], [SEM_SITE], quadrante)

    varredura = _rodar(
        monkeypatch, [_candidato("fsq_1")], [SEM_SITE], quadrante, fonte="foursquare"
    )
    assert varredura.status == StatusVarredura.CONCLUIDA


def test_fonte_desconhecida_falha_cedo(monkeypatch, quadrante):
    with pytest.raises(ValueError, match="Fonte desconhecida"):
        _rodar(monkeypatch, [_candidato()], [SEM_SITE], quadrante, fonte="waze")


def test_erro_de_busca_grava_o_custo_ja_consumido(monkeypatch, quadrante):
    """Requisição gasta tem de contar na franquia mesmo com varredura em erro."""
    from leads.sources.models import BuscaParcialError

    async def _coletar_falho(texto_query, celula, fonte=None, segmento=None):
        raise BuscaParcialError(total_requisicoes=2, candidatos=[])

    monkeypatch.setattr(captacao, "_coletar", _coletar_falho)

    with pytest.raises(BuscaParcialError):
        captacao.executar_varredura(
            segmento=Segmento.SALAO,
            segmento_rotulo=Segmento.SALAO.label,
            cidade="Maringá",
            estado="PR",
            quadrante=quadrante,
        )

    varredura = Varredura.objects.get()
    assert varredura.status == StatusVarredura.ERRO
    assert varredura.total_requisicoes == 2
    assert captacao.consumo_do_mes() == 2


def test_query_montada_com_segmento_e_cidade(monkeypatch, quadrante):
    varredura = _rodar(monkeypatch, [_candidato()], [SEM_SITE], quadrante)
    assert varredura.termo_busca == "Salão de beleza em Maringá PR"
