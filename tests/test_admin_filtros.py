"""Testes do layout de filtros do Admin no Incremento A2.1."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from leads.admin import ProspectAdmin, VarreduraAdmin
from leads.admin_cidades import ProspectCidadesFilter

pytestmark = pytest.mark.django_db


@pytest.fixture
def cliente_admin():
    operador = get_user_model().objects.create_superuser(
        username="admin-filtros",
        password="senha-teste",
    )
    cliente = Client()
    cliente.force_login(operador)
    return cliente


def test_prospect_admin_carrega_assets_de_filtros(cliente_admin):
    resposta = cliente_admin.get(reverse("admin:leads_prospect_changelist"))

    assert resposta.status_code == 200
    assert b"leads/admin_filtros.css" in resposta.content
    assert b"leads/admin_filtros.js" in resposta.content


def test_varredura_admin_carrega_assets_e_preserva_nova_captacao(cliente_admin):
    resposta = cliente_admin.get(reverse("admin:leads_varredura_changelist"))

    assert resposta.status_code == 200
    assert b"leads/admin_filtros.css" in resposta.content
    assert b"leads/admin_filtros.js" in resposta.content
    assert b"Nova Capta" in resposta.content


def test_prospect_admin_preserva_filtros_existentes():
    assert ProspectAdmin.list_filter[0].__name__ == "ProspectWhatsAppFilter"
    assert (
        ProspectAdmin.list_filter[1].__name__
        == "ProspectContatoWhatsAppFilter"
    )

    assert ProspectAdmin.list_filter[2:] == (
        "status_funil",
        "segmento",
        "nicho",
        "tem_site_real",
        "revisado_manualmente",
        ProspectCidadesFilter,
        "origem",
    )


def test_varredura_admin_preserva_filtros_existentes():
    assert VarreduraAdmin.list_filter == (
        "status",
        "fonte",
        "segmento",
        "nicho",
        "cidade",
    )


def test_assets_do_a2_1_ficam_restritos_a_prospect_e_varredura(cliente_admin):
    resposta = cliente_admin.get(reverse("admin:leads_interacao_changelist"))

    assert resposta.status_code == 200
    assert b"leads/admin_filtros.css" not in resposta.content
    assert b"leads/admin_filtros.js" not in resposta.content
