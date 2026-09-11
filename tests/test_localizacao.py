"""Localização factual: fonte, recaptura e edição manual no Admin."""

from __future__ import annotations

import math

from django.contrib import admin as django_admin

import pytest

from leads.admin import ProspectAdmin
from leads.filters.site_validator import SiteVerdict
from leads.models import (
    Nicho,
    OrigemLocalizacao,
    Prospect,
    Segmento,
    Varredura,
)
from leads.services import captacao
from leads.sources.models import ProspectCandidate

pytestmark = pytest.mark.django_db

SEM_SITE = SiteVerdict(tem_site_real=False, evidencia="teste")


@pytest.fixture
def nicho():
    return Nicho.objects.get_or_create(codigo="beleza", defaults={"nome": "Beleza"})[0]


@pytest.fixture
def varredura(nicho):
    return Varredura.objects.create(
        termo_busca="salão em Goiânia GO",
        segmento=Segmento.SALAO,
        nicho=nicho,
        fonte="FOURSQUARE",
        cidade="Goiânia",
        estado="GO",
    )


def _candidato(**extra):
    campos = {
        "origem": "FOURSQUARE",
        "origem_id": "fsq-localizacao",
        "nome": "Salão Localização",
        "endereco": "Rua da Fonte, 12",
        "bairro": "Centro",
        "cidade": "Curitiba",
        "estado": "PR",
        "pais": "BR",
        "cep": "80000-000",
        "latitude": -25.4284,
        "longitude": -49.2733,
    }
    campos.update(extra)
    return ProspectCandidate(**campos)


def _persistir(varredura, candidato):
    captacao._persistir(varredura, [candidato], [SEM_SITE], site_confiavel=False)
    return Prospect.objects.get(origem_id=candidato.origem_id)


def test_persiste_localizacao_da_fonte_em_vez_do_alvo_da_varredura(
    varredura,
):
    prospect = _persistir(varredura, _candidato())

    assert (prospect.cidade, prospect.estado) == ("Curitiba", "PR")
    assert (varredura.cidade, varredura.estado) == ("Goiânia", "GO")
    assert prospect.endereco == "Rua da Fonte, 12"
    assert prospect.bairro == "Centro"
    assert prospect.pais == "BR"
    assert prospect.cep == "80000-000"
    assert prospect.latitude == -25.4284
    assert prospect.longitude == -49.2733
    assert prospect.origem_localizacao == OrigemLocalizacao.FONTE


def test_varredura_nunca_e_fallback_de_localizacao(varredura):
    candidato = ProspectCandidate(
        origem="FOURSQUARE",
        origem_id="fsq-sem-localizacao",
        nome="Salão Sem Localização",
    )

    prospect = _persistir(varredura, candidato)

    assert prospect.endereco is None
    assert prospect.cidade is None
    assert prospect.estado is None
    assert prospect.origem_localizacao == OrigemLocalizacao.DESCONHECIDA


def test_recaptura_atualiza_so_campos_geograficos_recebidos(varredura):
    prospect = _persistir(varredura, _candidato())
    parcial = _candidato(
        cidade="Londrina",
        endereco=None,
        bairro=None,
        estado=None,
        pais=None,
        cep=None,
        latitude=None,
        longitude=None,
    )

    _persistir(varredura, parcial)
    prospect.refresh_from_db()

    assert prospect.cidade == "Londrina"
    assert prospect.endereco == "Rua da Fonte, 12"
    assert prospect.bairro == "Centro"
    assert prospect.estado == "PR"
    assert prospect.latitude == -25.4284
    assert prospect.longitude == -49.2733
    assert prospect.origem_localizacao == OrigemLocalizacao.FONTE


@pytest.mark.parametrize(
    ("latitude", "longitude", "esperado"),
    [
        (-22.9068, -43.1729, (-22.9068, -43.1729)),
        (-22.9068, None, (None, None)),
        (None, -43.1729, (None, None)),
        (True, -43.1729, (None, None)),
        (91.0, -43.1729, (None, None)),
        (-22.9068, -181.0, (None, None)),
        (math.nan, -43.1729, (None, None)),
        (-22.9068, math.inf, (None, None)),
    ],
)
def test_criacao_so_persiste_par_de_coordenadas_numerico_valido(
    varredura,
    latitude,
    longitude,
    esperado,
):
    prospect = _persistir(
        varredura,
        _candidato(
            origem_id=f"coordenadas-criacao-{latitude!s}-{longitude!s}",
            latitude=latitude,
            longitude=longitude,
        ),
    )

    assert (prospect.latitude, prospect.longitude) == esperado


@pytest.mark.parametrize(
    ("latitude", "longitude"),
    [(-22.9068, None), (None, -43.1729), (True, -43.1729), (91.0, -43.1729)],
)
def test_recaptura_com_par_incompleto_ou_invalido_preserva_coordenadas_conhecidas(
    varredura,
    latitude,
    longitude,
):
    prospect = _persistir(varredura, _candidato(origem_id="coordenadas-recaptura"))

    _persistir(
        varredura,
        _candidato(
            origem_id="coordenadas-recaptura",
            endereco=None,
            bairro=None,
            cidade=None,
            estado=None,
            pais=None,
            cep=None,
            latitude=latitude,
            longitude=longitude,
        ),
    )
    prospect.refresh_from_db()

    assert (prospect.latitude, prospect.longitude) == (-25.4284, -49.2733)


def test_recaptura_com_par_valido_substitui_as_duas_coordenadas_juntas(varredura):
    prospect = _persistir(varredura, _candidato(origem_id="coordenadas-validas"))

    _persistir(
        varredura,
        _candidato(
            origem_id="coordenadas-validas",
            endereco=None,
            bairro=None,
            cidade=None,
            estado=None,
            pais=None,
            cep=None,
            latitude=-22.9068,
            longitude=-43.1729,
        ),
    )
    prospect.refresh_from_db()

    assert (prospect.latitude, prospect.longitude) == (-22.9068, -43.1729)


def test_recaptura_nao_apaga_localizacao_manual_ausente_na_fonte(varredura):
    prospect = _persistir(varredura, _candidato())
    prospect.cidade = "Cidade Manual"
    prospect.origem_localizacao = OrigemLocalizacao.MANUAL
    prospect.save()

    _persistir(
        varredura,
        _candidato(
            endereco=None,
            bairro=None,
            cidade=None,
            estado=None,
            pais=None,
            cep=None,
            latitude=None,
            longitude=None,
        ),
    )
    prospect.refresh_from_db()

    assert prospect.cidade == "Cidade Manual"
    assert prospect.endereco == "Rua da Fonte, 12"
    assert prospect.origem_localizacao == OrigemLocalizacao.MANUAL


def test_recaptura_nao_altera_localizacao_manual_mesmo_com_dados_da_fonte(varredura):
    prospect = _persistir(varredura, _candidato(origem_id="manual-com-fonte"))
    prospect.cidade = "Cidade Manual"
    prospect.origem_localizacao = OrigemLocalizacao.MANUAL
    prospect.save()

    _persistir(
        varredura,
        _candidato(
            origem_id="manual-com-fonte",
            endereco="Rua da Fonte Atualizada, 99",
            cidade="Cidade da Fonte",
            latitude=-22.9068,
            longitude=-43.1729,
        ),
    )
    prospect.refresh_from_db()

    assert prospect.endereco == "Rua da Fonte, 12"
    assert prospect.cidade == "Cidade Manual"
    assert (prospect.latitude, prospect.longitude) == (-25.4284, -49.2733)
    assert prospect.origem_localizacao == OrigemLocalizacao.MANUAL


def test_recaptura_legada_com_string_vazia_nao_apaga_localizacao(varredura):
    prospect = _persistir(varredura, _candidato())

    _persistir(
        varredura,
        _candidato(
            endereco="",
            bairro="",
            cidade="",
            estado="",
            pais="",
            cep="",
            latitude=None,
            longitude=None,
        ),
    )
    prospect.refresh_from_db()

    assert prospect.endereco == "Rua da Fonte, 12"
    assert prospect.cidade == "Curitiba"
    assert prospect.origem_localizacao == OrigemLocalizacao.FONTE


def test_historico_desconhecido_parcialmente_enriquecido_permanece_desconhecido(
    varredura,
):
    prospect = Prospect.objects.create(
        origem="FOURSQUARE",
        origem_id="historico-parcial",
        nome="Histórico Parcial",
        nicho=varredura.nicho,
        cidade="Maringá",
        estado="PR",
        origem_localizacao=OrigemLocalizacao.DESCONHECIDA,
    )

    _persistir(
        varredura,
        _candidato(
            origem_id="historico-parcial",
            endereco="Rua Declarada, 1",
            bairro=None,
            cidade=None,
            estado=None,
            pais=None,
            cep=None,
            latitude=None,
            longitude=None,
        ),
    )
    prospect.refresh_from_db()

    assert prospect.endereco == "Rua Declarada, 1"
    assert (prospect.cidade, prospect.estado) == ("Maringá", "PR")
    assert prospect.origem_localizacao == OrigemLocalizacao.DESCONHECIDA


def test_historico_desconhecido_totalmente_substituido_pela_fonte_vira_fonte(
    varredura,
):
    prospect = Prospect.objects.create(
        origem="FOURSQUARE",
        origem_id="historico-substituido",
        nome="Histórico Substituído",
        nicho=varredura.nicho,
        endereco="Endereço antigo",
        cidade="Cidade antiga",
        estado="XX",
        origem_localizacao=OrigemLocalizacao.DESCONHECIDA,
    )

    _persistir(varredura, _candidato(origem_id="historico-substituido"))
    prospect.refresh_from_db()

    assert prospect.endereco == "Rua da Fonte, 12"
    assert (prospect.cidade, prospect.estado) == ("Curitiba", "PR")
    assert prospect.origem_localizacao == OrigemLocalizacao.FONTE


def test_historico_desconhecido_sem_localizacao_vira_fonte_com_resposta_parcial(
    varredura,
):
    prospect = Prospect.objects.create(
        origem="FOURSQUARE",
        origem_id="historico-vazio",
        nome="Histórico sem localização",
        nicho=varredura.nicho,
        origem_localizacao=OrigemLocalizacao.DESCONHECIDA,
    )

    _persistir(
        varredura,
        _candidato(
            origem_id="historico-vazio",
            endereco=None,
            bairro=None,
            cidade="Curitiba",
            estado=None,
            pais=None,
            cep=None,
            latitude=None,
            longitude=None,
        ),
    )
    prospect.refresh_from_db()

    assert prospect.cidade == "Curitiba"
    assert prospect.origem_localizacao == OrigemLocalizacao.FONTE


def test_admin_marca_localizacao_como_manual_quando_campo_geografico_muda(
    varredura,
):
    prospect = _persistir(varredura, _candidato())

    class Formulario:
        changed_data = ["cidade"]

    painel = ProspectAdmin(Prospect, django_admin.site)
    prospect.cidade = "Cidade Corrigida"
    painel.save_model(None, prospect, Formulario(), change=True)
    prospect.refresh_from_db()

    assert prospect.origem_localizacao == OrigemLocalizacao.MANUAL


def test_candidato_google_antigo_permanece_compativel():
    candidato = ProspectCandidate(
        origem="GOOGLE_PLACES",
        origem_id="google-antigo",
        nome="Salão Google",
        endereco="Rua Google, 1",
    )

    assert candidato.endereco == "Rua Google, 1"
    assert candidato.cidade is None
    assert candidato.latitude is None
