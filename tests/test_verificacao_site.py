"""Testes da regra "fonte não confiável não qualifica sozinha".

O que está sendo protegido aqui é uma afirmação sobre o mundo. Quando o
Google devolve `websiteUri` vazio, isso é evidência de que o negócio não tem
site. Quando a Foursquare devolve vazio, isso é a Foursquare não ter o dado —
93% dos 400 prospects de Maringá. Tratar os dois como a mesma coisa enche a
fila de curadoria de palpite disfarçado de lead qualificado.

Nenhum teste faz chamada real: a parte de rede é substituída por stub, no
mesmo padrão de `test_captacao.py`.
"""

from __future__ import annotations

import pytest

from leads.filters.site_validator import EVIDENCIA_SEM_URL, SiteVerdict
from leads.models import (
    Nicho,
    Prospect,
    ProspectVerificacao,
    Quadrante,
    Segmento,
    StatusFunil,
)
from leads.services import captacao
from leads.sources.foursquare import FoursquareSource
from leads.sources.google_places import GooglePlacesSource
from leads.sources.models import ProspectCandidate

pytestmark = pytest.mark.django_db

# O veredito exato que a fonte sem dado produz: False, mas sem nada checado.
SEM_URL = SiteVerdict(tem_site_real=False, evidencia=EVIDENCIA_SEM_URL)
# Veredito de URL realmente seguida e reprovada.
CHECADO_REPROVADO = SiteVerdict(tem_site_real=False, evidencia="http 404")
COM_SITE = SiteVerdict(tem_site_real=True, evidencia="site ativo")


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


def _fake_coletar(candidatos, vereditos):
    async def _coletar(texto_query, celula, fonte=None, segmento=None, nicho_codigo=None):
        return captacao.Coleta(
            candidatos=candidatos, requisicoes=1, vereditos=vereditos
        )

    return _coletar


def _candidato(origem, origem_id="x1", *, telefone="", website_url=""):
    return ProspectCandidate(
        origem=origem,
        origem_id=origem_id,
        nome="Salão Teste",
        endereco="Rua X, Maringá",
        telefone=telefone,
        website_url=website_url,
    )


def _rodar(monkeypatch, candidatos, vereditos, quadrante, fonte):
    monkeypatch.setattr(captacao, "_coletar", _fake_coletar(candidatos, vereditos))
    return captacao.executar_varredura(
        nicho=Nicho.objects.get(codigo="beleza"),
        segmento=Segmento.SALAO,
        consulta="Salão de beleza em Maringá PR",
        cidade="Maringá",
        estado="PR",
        quadrante=quadrante,
        fonte=fonte,
    )


# --- A política mora na fonte -----------------------------------------------


def test_google_declara_site_confiavel():
    assert GooglePlacesSource.SITE_CONFIAVEL is True


def test_foursquare_declara_site_nao_confiavel():
    """Se alguém inverter isto, os 373 palpites voltam a virar lead aprovado."""
    assert FoursquareSource.SITE_CONFIAVEL is False


# --- Foursquare: nunca aprovado automaticamente ------------------------------


def test_foursquare_sem_url_nasce_pendente_e_com_site_desconhecido(
    monkeypatch, quadrante
):
    _rodar(
        monkeypatch,
        [_candidato("FOURSQUARE", "fsq1")],
        [SEM_URL],
        quadrante,
        "foursquare",
    )

    p = Prospect.objects.get()
    assert p.status_funil == StatusFunil.VERIFICAR_SITE
    assert p.ativo_no_funil is False
    # NULL = desconhecido. Gravar False seria afirmar um fato não apurado.
    assert p.tem_site_real is None
    assert p.e_alvo is False
    assert p.pendente_de_verificacao is True


def test_foursquare_com_url_checada_tambem_fica_pendente(monkeypatch, quadrante):
    """Mesmo com URL seguida e reprovada, a fonte não aprova sozinha."""
    _rodar(
        monkeypatch,
        [_candidato("FOURSQUARE", "fsq2", website_url="https://morto.example")],
        [CHECADO_REPROVADO],
        quadrante,
        "foursquare",
    )

    p = Prospect.objects.get()
    assert p.status_funil == StatusFunil.VERIFICAR_SITE
    assert p.e_alvo is False
    # A evidência apurada é preservada — foi checagem de verdade.
    assert p.tem_site_real is False
    assert p.site_evidencia == "http 404"


@pytest.mark.parametrize("veredito", [SEM_URL, CHECADO_REPROVADO])
def test_nenhum_prospect_foursquare_sai_aprovado(monkeypatch, quadrante, veredito):
    """O critério de aceite, direto: independente do campo site."""
    _rodar(
        monkeypatch,
        [_candidato("FOURSQUARE", "fsq3")],
        [veredito],
        quadrante,
        "foursquare",
    )

    p = Prospect.objects.get()
    assert p.status_funil != StatusFunil.INICIAR
    assert p.ativo_no_funil is False
    assert p.revisado_manualmente is False
    assert p.e_alvo is False


def test_tag_de_verificacao_mostra_origem_e_motivo(monkeypatch, quadrante):
    _rodar(
        monkeypatch,
        [_candidato("FOURSQUARE", "fsq4")],
        [SEM_URL],
        quadrante,
        "foursquare",
    )

    assert (
        Prospect.objects.get().tag_verificacao
        == "Foursquare · site desconhecido — verificar manualmente"
    )


def test_tag_distingue_checado_de_desconhecido(monkeypatch, quadrante):
    _rodar(
        monkeypatch,
        [_candidato("FOURSQUARE", "fsq5", website_url="https://morto.example")],
        [CHECADO_REPROVADO],
        quadrante,
        "foursquare",
    )

    assert "site checado, confirmar" in Prospect.objects.get().tag_verificacao


def test_recaptura_foursquare_nao_promove_sozinha(monkeypatch, quadrante):
    """Recaptura não pode ser a porta dos fundos para sair da fila."""
    for _ in range(2):
        _rodar(
            monkeypatch,
            [_candidato("FOURSQUARE", "fsq6")],
            [SEM_URL],
            quadrante,
            "foursquare",
        )

    assert Prospect.objects.count() == 1
    p = Prospect.objects.get()
    assert p.status_funil == StatusFunil.VERIFICAR_SITE
    assert p.tem_site_real is None


def test_recaptura_respeita_verificacao_ja_feita(monkeypatch, quadrante):
    """Quem já foi confirmado à mão não volta para a fila."""
    _rodar(
        monkeypatch,
        [_candidato("FOURSQUARE", "fsq7")],
        [SEM_URL],
        quadrante,
        "foursquare",
    )

    p = Prospect.objects.get()
    p.status_funil = StatusFunil.INICIAR
    p.ativo_no_funil = True
    p.revisado_manualmente = True
    p.save()

    _rodar(
        monkeypatch,
        [_candidato("FOURSQUARE", "fsq7")],
        [SEM_URL],
        quadrante,
        "foursquare",
    )

    p.refresh_from_db()
    assert p.status_funil == StatusFunil.INICIAR
    assert p.ativo_no_funil is True


# --- Google: zero regressão --------------------------------------------------


def test_google_sem_url_continua_qualificando(monkeypatch, quadrante):
    """O comportamento da Fase 1, intacto: aqui vazio SIGNIFICA sem site."""
    _rodar(
        monkeypatch,
        [_candidato("GOOGLE_PLACES", "g1")],
        [SEM_URL],
        quadrante,
        "google_places",
    )

    p = Prospect.objects.get()
    assert p.status_funil == StatusFunil.NOVO
    assert p.tem_site_real is False
    assert p.e_alvo is True
    assert p.pendente_de_verificacao is False
    assert p.tag_verificacao == ""


def test_google_com_site_segue_fora_do_banco(monkeypatch, quadrante):
    _rodar(
        monkeypatch,
        [_candidato("GOOGLE_PLACES", "g2", website_url="https://salao.com.br")],
        [COM_SITE],
        quadrante,
        "google_places",
    )

    assert Prospect.objects.count() == 0


def test_recaptura_google_nao_mexe_em_status(monkeypatch, quadrante):
    """Regressão: a recaptura do Google nunca tocou status_funil."""
    _rodar(
        monkeypatch,
        [_candidato("GOOGLE_PLACES", "g3")],
        [SEM_URL],
        quadrante,
        "google_places",
    )

    p = Prospect.objects.get()
    p.status_funil = StatusFunil.EM_ANDAMENTO
    p.ativo_no_funil = True
    p.save()

    _rodar(
        monkeypatch,
        [_candidato("GOOGLE_PLACES", "g3")],
        [SEM_URL],
        quadrante,
        "google_places",
    )

    p.refresh_from_db()
    assert p.status_funil == StatusFunil.EM_ANDAMENTO
    assert p.ativo_no_funil is True


# --- Fila de verificação: recorte e ordenação --------------------------------


def _semear_fila(monkeypatch, quadrante):
    candidatos = [
        _candidato("FOURSQUARE", "sem_tel_a", telefone=""),
        _candidato("FOURSQUARE", "com_cel", telefone="(44) 99912-0926"),
        _candidato("FOURSQUARE", "sem_tel_b", telefone=""),
    ]
    _rodar(monkeypatch, candidatos, [SEM_URL] * 3, quadrante, "foursquare")


def test_fila_so_mostra_pendentes(monkeypatch, quadrante):
    _semear_fila(monkeypatch, quadrante)
    _rodar(
        monkeypatch,
        [_candidato("GOOGLE_PLACES", "g4")],
        [SEM_URL],
        quadrante,
        "google_places",
    )

    from leads.admin import ProspectVerificacaoAdmin
    from django.contrib import admin as django_admin

    fila = ProspectVerificacaoAdmin(ProspectVerificacao, django_admin.site)
    qs = fila.get_queryset(None)

    assert qs.count() == 3
    assert not qs.filter(origem="GOOGLE_PLACES").exists()


def test_fila_ordena_celular_primeiro(monkeypatch, quadrante):
    """Sem rating/stats (campos pagos), o telefone é o único critério útil."""
    _semear_fila(monkeypatch, quadrante)

    from leads.admin import ProspectVerificacaoAdmin
    from django.contrib import admin as django_admin

    fila = ProspectVerificacaoAdmin(ProspectVerificacao, django_admin.site)
    ordenados = list(fila.get_queryset(None))

    assert ordenados[0].origem_id == "com_cel"
    assert all(not p.telefone_e_celular for p in ordenados[1:])


def test_fila_nao_prioriza_fixo(monkeypatch, quadrante):
    """O bug que motivou a mudança: 101 dos 158 'com telefone' eram fixos.

    Fixo não abre conversa por mensagem, que é o único canal da abordagem.
    Ordenar por campo preenchido colocava uma centena deles à frente de
    prospect com celular de verdade.
    """
    candidatos = [
        _candidato("FOURSQUARE", "a_fixo", telefone="(44) 3244-6413"),
        _candidato("FOURSQUARE", "z_celular", telefone="(44) 99912-0926"),
    ]
    _rodar(monkeypatch, candidatos, [SEM_URL] * 2, quadrante, "foursquare")

    from leads.admin import ProspectVerificacaoAdmin
    from django.contrib import admin as django_admin

    fila = ProspectVerificacaoAdmin(ProspectVerificacao, django_admin.site)
    ordenados = list(fila.get_queryset(None))

    # O fixo vem primeiro no alfabeto e mesmo assim perde do celular.
    assert [p.origem_id for p in ordenados] == ["z_celular", "a_fixo"]


# --- A trava da aprovação em lote --------------------------------------------


def test_aprovar_em_lote_nao_leva_pendente(monkeypatch, quadrante):
    """O caminho real do acidente: 'selecionar tudo' no Admin."""
    _semear_fila(monkeypatch, quadrante)
    _rodar(
        monkeypatch,
        [_candidato("GOOGLE_PLACES", "g5")],
        [SEM_URL],
        quadrante,
        "google_places",
    )

    from leads.admin import ProspectAdmin
    from django.contrib import admin as django_admin

    class _Request:
        """Request mínimo — a action só usa `message_user`."""

    pedido = _Request()
    painel = ProspectAdmin(Prospect, django_admin.site)
    painel.message_user = lambda *a, **kw: None

    painel.aprovar_para_funil(pedido, Prospect.objects.all())

    assert Prospect.objects.filter(status_funil=StatusFunil.INICIAR).count() == 1
    assert Prospect.objects.get(status_funil=StatusFunil.INICIAR).origem_id == "g5"
    assert (
        Prospect.objects.filter(status_funil=StatusFunil.VERIFICAR_SITE).count() == 3
    )
    assert not Prospect.objects.filter(
        origem="FOURSQUARE", ativo_no_funil=True
    ).exists()
