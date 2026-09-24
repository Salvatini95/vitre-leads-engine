"""WhatsApp manual de entregas na lista normal de Prospects."""

from urllib.parse import parse_qs, urlparse

import pytest
from django.contrib import admin as django_admin
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from leads.admin import ProspectAdmin
from leads.models import Interacao, Nicho, Origem, Prospect, Segmento
from leads.utils.telefone import TipoTelefone


pytestmark = pytest.mark.django_db


@pytest.fixture
def cliente_admin():
    usuario = get_user_model().objects.create_superuser(
        username="admin-whatsapp-entregas",
        password="senha-teste",
    )

    cliente = Client()
    cliente.force_login(usuario)

    return cliente


@pytest.fixture
def nicho_autopecas():
    return Nicho.objects.create(
        codigo="autopecas",
        nome="Autopeças",
        ativo=True,
    )


@pytest.fixture
def prospect_celular(nicho_autopecas):
    return Prospect.objects.create(
        origem=Origem.FOURSQUARE,
        origem_id="whatsapp-entregas-1",
        nome="Auto Peças Teste",
        segmento=Segmento.OUTRO,
        nicho=nicho_autopecas,
        cidade="Maringá",
        estado="PR",
        telefone="+5544999120926",
        telefone_tipo=TipoTelefone.CELULAR,
    )


def test_lista_normal_exibe_coluna_whatsapp():
    painel = ProspectAdmin(
        Prospect,
        django_admin.site,
    )

    assert "abrir_whatsapp_entregas" in painel.list_display


def test_botao_so_aparece_para_celular_valido(
    prospect_celular,
):
    painel = ProspectAdmin(
        Prospect,
        django_admin.site,
    )

    html = str(
        painel.abrir_whatsapp_entregas(
            prospect_celular
        )
    )

    assert "Abrir WhatsApp" in html
    assert "whatsapp-entregas" in html

    prospect_celular.telefone_tipo = TipoTelefone.FIXO

    assert (
        painel.abrir_whatsapp_entregas(
            prospect_celular
        )
        == "—"
    )


def test_tela_permite_personalizar_mensagem(
    cliente_admin,
    prospect_celular,
):
    resposta = cliente_admin.get(
        reverse(
            "admin:leads_prospect_whatsapp_entregas",
            args=[prospect_celular.pk],
        )
    )

    assert resposta.status_code == 200

    assert b'name="mensagem"' in resposta.content

    assert (
        "autopeças da região".encode()
        in resposta.content
    )


def test_post_abre_whatsapp_sem_alterar_prospect(
    cliente_admin,
    prospect_celular,
):
    status_antes = prospect_celular.status_funil

    revisado_antes = (
        prospect_celular.revisado_manualmente
    )

    interacoes_antes = Interacao.objects.filter(
        prospect=prospect_celular
    ).count()

    mensagem = (
        "Mensagem comercial personalizada para teste."
    )

    resposta = cliente_admin.post(
        reverse(
            "admin:leads_prospect_whatsapp_entregas",
            args=[prospect_celular.pk],
        ),
        {
            "mensagem": mensagem,
        },
    )

    assert resposta.status_code == 302

    destino = urlparse(
        resposta["Location"]
    )

    assert destino.netloc == "wa.me"

    assert (
        destino.path
        == "/5544999120926"
    )

    assert (
        parse_qs(destino.query)["text"]
        == [mensagem]
    )

    prospect_celular.refresh_from_db()

    assert (
        prospect_celular.status_funil
        == status_antes
    )

    assert (
        prospect_celular.revisado_manualmente
        == revisado_antes
    )

    assert (
        Interacao.objects.filter(
            prospect=prospect_celular
        ).count()
        == interacoes_antes
    )


def test_mensagem_vazia_nao_abre_whatsapp(
    cliente_admin,
    prospect_celular,
):
    resposta = cliente_admin.post(
        reverse(
            "admin:leads_prospect_whatsapp_entregas",
            args=[prospect_celular.pk],
        ),
        {
            "mensagem": "   ",
        },
    )

    assert resposta.status_code == 200

    assert (
        b"Digite uma mensagem antes de abrir o WhatsApp."
        in resposta.content
    )
