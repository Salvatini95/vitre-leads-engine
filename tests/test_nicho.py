"""Testes da estrutura comercial de nicho.

Nicho não substitui Segmento: os testes de captação existentes continuam
guardando o comportamento técnico e de copy. Estes verificam apenas a nova
relação e o backfill histórico deliberadamente restrito.
"""

from __future__ import annotations

import importlib

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection
from django.db.migrations.executor import MigrationExecutor
from django.db.models.deletion import ProtectedError
from django.test import Client
from django.urls import reverse

from leads.models import Nicho, Origem, Prospect, Segmento, Varredura

pytestmark = pytest.mark.django_db


def _varredura(*, nicho: Nicho | None = None) -> Varredura:
    return Varredura.objects.create(
        termo_busca="salão de beleza em Maringá PR",
        segmento=Segmento.SALAO,
        nicho=nicho,
        cidade="Maringá",
        estado="PR",
    )


def test_cria_nicho_com_codigo_estavel():
    nicho = Nicho.objects.create(codigo="motoboys", nome="Motoboys")

    assert nicho.codigo == "motoboys"
    assert nicho.nome == "Motoboys"
    assert nicho.ativo is True


def test_codigo_do_nicho_e_unico():
    Nicho.objects.create(codigo="empresas-sistema", nome="Empresas para sistema")

    with pytest.raises(IntegrityError):
        Nicho.objects.create(codigo="empresas-sistema", nome="Outro nome")


def test_prospect_pertence_a_um_nicho():
    beleza = Nicho.objects.get(codigo="beleza")
    prospect = Prospect.objects.create(
        origem=Origem.MANUAL,
        origem_id="manual-nicho-1",
        nome="Prospect de teste",
        nicho=beleza,
    )

    assert prospect.nicho == beleza
    assert list(beleza.prospects.all()) == [prospect]


def test_varredura_pode_registrar_nicho():
    beleza = Nicho.objects.get(codigo="beleza")
    varredura = _varredura(nicho=beleza)

    assert varredura.nicho == beleza
    assert list(beleza.varreduras.all()) == [varredura]


def test_admin_filtra_prospects_por_nicho_no_changelist():
    beleza = Nicho.objects.get(codigo="beleza")
    motoboys = Nicho.objects.create(codigo="motoboys", nome="Motoboys")
    Prospect.objects.create(
        origem=Origem.MANUAL,
        origem_id="admin-beleza",
        nome="Prospect Beleza",
        nicho=beleza,
    )
    Prospect.objects.create(
        origem=Origem.MANUAL,
        origem_id="admin-motoboy",
        nome="Prospect Motoboy",
        nicho=motoboys,
    )
    operador = get_user_model().objects.create_superuser(
        username="admin-nicho",
        password="senha-teste",
    )
    cliente = Client()
    cliente.force_login(operador)

    resposta = cliente.get(
        reverse("admin:leads_prospect_changelist"),
        {"nicho__id__exact": beleza.pk},
    )

    assert resposta.status_code == 200
    assert "nicho__id__exact" in resposta.context["cl"].get_filters_params()
    assert list(resposta.context["cl"].queryset.values_list("nome", flat=True)) == [
        "Prospect Beleza"
    ]


def test_nicho_em_uso_por_prospect_nao_pode_ser_apagado():
    beleza = Nicho.objects.get(codigo="beleza")
    Prospect.objects.create(
        origem=Origem.MANUAL,
        origem_id="manual-nicho-protect",
        nome="Prospect protegido",
        nicho=beleza,
    )

    with pytest.raises(ProtectedError):
        beleza.delete()


def test_nicho_em_uso_por_varredura_nao_pode_ser_apagado():
    beleza = Nicho.objects.get(codigo="beleza")
    _varredura(nicho=beleza)

    with pytest.raises(ProtectedError):
        beleza.delete()


@pytest.mark.django_db(transaction=True)
def test_migration_associa_somente_o_lote_historico_com_evidencia():
    """O backfill usa a varredura de origem, nunca o segmento do prospect."""
    anterior = [("leads", "0010_alter_prospect_status_funil")]
    atual = [("leads", "0011_nicho_prospect_nicho_varredura_nicho")]
    leaf_atual = [("leads", "0012_localizacao_factual_do_prospect")]

    try:
        executor = MigrationExecutor(connection)
        executor.migrate(anterior)
        apps_anteriores = executor.loader.project_state(anterior).apps

        ProspectAntigo = apps_anteriores.get_model("leads", "Prospect")
        VarreduraAntiga = apps_anteriores.get_model("leads", "Varredura")

        varredura_comercial = VarreduraAntiga.objects.create(
            termo_busca="Salão de beleza em Maringá PR",
            segmento="SALAO",
            fonte="FOURSQUARE",
            cidade="Maringá",
            estado="PR",
        )
        prospect_corrigido = ProspectAntigo.objects.create(
            origem="FOURSQUARE",
            origem_id="historico-comprovado",
            nome="Prospect historicamente comprovado",
            segmento="OUTRO",
            varredura=varredura_comercial,
        )
        varredura_operacional = VarreduraAntiga.objects.create(
            termo_busca="recheca de fechamento (verificar_fechados)",
            segmento="OUTRO",
            fonte="FOURSQUARE",
            cidade="Maringá",
            estado="PR",
        )

        executor = MigrationExecutor(connection)
        executor.migrate(atual)
        apps_atuais = executor.loader.project_state(atual).apps

        NichoNovo = apps_atuais.get_model("leads", "Nicho")
        ProspectNovo = apps_atuais.get_model("leads", "Prospect")
        VarreduraNova = apps_atuais.get_model("leads", "Varredura")

        beleza = NichoNovo.objects.get(codigo="beleza")
        prospect = ProspectNovo.objects.get(pk=prospect_corrigido.pk)
        operacional = VarreduraNova.objects.get(pk=varredura_operacional.pk)

        assert prospect.nicho_id == beleza.pk
        assert operacional.nicho_id is None

        # Mantém a referência carregada para o lint/test detectar que a varredura
        # comercial também foi marcada, sem depender de segmento do prospect.
        assert VarreduraNova.objects.get(pk=varredura_comercial.pk).nicho_id == beleza.pk

        # Importar o módulo garante que a função RunPython esteja no artefato da
        # migration, além de ter sido executada pelo MigrationExecutor acima.
        migration = importlib.import_module("leads.migrations.0011_nicho_prospect_nicho_varredura_nicho")
        assert callable(migration.criar_nicho_e_fazer_backfill)
    finally:
        MigrationExecutor(connection).migrate(leaf_atual)
