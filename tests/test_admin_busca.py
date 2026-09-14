"""Testes da busca digitável dos changelists de Prospect e Varredura."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from leads.models import Nicho, Origem, Prospect, Segmento, Varredura

pytestmark = pytest.mark.django_db


@pytest.fixture
def cliente_admin():
    operador = get_user_model().objects.create_superuser(
        username="admin-busca",
        password="senha-teste",
    )
    cliente = Client()
    cliente.force_login(operador)
    return cliente


@pytest.fixture
def dados_busca():
    beleza, _ = Nicho.objects.get_or_create(
        codigo="beleza",
        defaults={"nome": "Beleza"},
    )
    motofrete = Nicho.objects.create(
        codigo="entrega-local",
        nome="Motofrete",
    )

    prospect_maringa = Prospect.objects.create(
        origem=Origem.MANUAL,
        origem_id="busca-prospect-maringa",
        nome="Expresso Paraná",
        cidade="Maringá",
        nicho=motofrete,
        segmento=Segmento.OUTRO,
    )
    prospect_londrina = Prospect.objects.create(
        origem=Origem.MANUAL,
        origem_id="busca-prospect-londrina",
        nome="Beleza Central",
        cidade="Londrina",
        nicho=beleza,
        segmento=Segmento.SALAO,
    )

    varredura_maringa = Varredura.objects.create(
        termo_busca="motofrete em Maringá PR",
        segmento=Segmento.OUTRO,
        nicho=motofrete,
        cidade="Maringá",
        estado="PR",
    )
    varredura_londrina = Varredura.objects.create(
        termo_busca="salão de beleza em Londrina PR",
        segmento=Segmento.SALAO,
        nicho=beleza,
        cidade="Londrina",
        estado="PR",
    )

    return {
        "motofrete": motofrete,
        "prospect_maringa": prospect_maringa,
        "prospect_londrina": prospect_londrina,
        "varredura_maringa": varredura_maringa,
        "varredura_londrina": varredura_londrina,
    }


def _objetos_do_changelist(resposta):
    assert resposta.status_code == 200
    return list(resposta.context["cl"].queryset)


def test_admin_busca_prospect_por_nome(cliente_admin, dados_busca):
    resposta = cliente_admin.get(
        reverse("admin:leads_prospect_changelist"),
        {"q": "Expresso"},
    )
    assert _objetos_do_changelist(resposta) == [dados_busca["prospect_maringa"]]


def test_admin_busca_prospect_por_cidade(cliente_admin, dados_busca):
    resposta = cliente_admin.get(
        reverse("admin:leads_prospect_changelist"),
        {"q": "Londrina"},
    )
    assert _objetos_do_changelist(resposta) == [dados_busca["prospect_londrina"]]


def test_admin_busca_prospect_por_nicho_nome_e_codigo(cliente_admin, dados_busca):
    url = reverse("admin:leads_prospect_changelist")

    por_nome = cliente_admin.get(url, {"q": "Motofrete"})
    por_codigo = cliente_admin.get(url, {"q": "entrega-local"})

    esperado = [dados_busca["prospect_maringa"]]
    assert _objetos_do_changelist(por_nome) == esperado
    assert _objetos_do_changelist(por_codigo) == esperado


def test_admin_busca_varredura_por_termo(cliente_admin, dados_busca):
    resposta = cliente_admin.get(
        reverse("admin:leads_varredura_changelist"),
        {"q": "motofrete"},
    )
    assert _objetos_do_changelist(resposta) == [dados_busca["varredura_maringa"]]


def test_admin_busca_varredura_por_cidade(cliente_admin, dados_busca):
    resposta = cliente_admin.get(
        reverse("admin:leads_varredura_changelist"),
        {"q": "Londrina"},
    )
    assert _objetos_do_changelist(resposta) == [dados_busca["varredura_londrina"]]


def test_admin_busca_varredura_por_nicho_nome_e_codigo(cliente_admin, dados_busca):
    url = reverse("admin:leads_varredura_changelist")

    por_nome = cliente_admin.get(url, {"q": "Motofrete"})
    por_codigo = cliente_admin.get(url, {"q": "entrega-local"})

    esperado = [dados_busca["varredura_maringa"]]
    assert _objetos_do_changelist(por_nome) == esperado
    assert _objetos_do_changelist(por_codigo) == esperado


def test_busca_prospect_compoe_com_filtro_existente(cliente_admin, dados_busca):
    resposta = cliente_admin.get(
        reverse("admin:leads_prospect_changelist"),
        {
            "q": "Maringá",
            "nicho__id__exact": dados_busca["motofrete"].pk,
        },
    )
    assert _objetos_do_changelist(resposta) == [dados_busca["prospect_maringa"]]


def test_busca_varredura_compoe_com_filtro_existente(cliente_admin, dados_busca):
    resposta = cliente_admin.get(
        reverse("admin:leads_varredura_changelist"),
        {
            "q": "Maringá",
            "nicho__id__exact": dados_busca["motofrete"].pk,
        },
    )
    assert _objetos_do_changelist(resposta) == [dados_busca["varredura_maringa"]]
