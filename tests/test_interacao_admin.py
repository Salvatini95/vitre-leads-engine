"""Acessibilidade dos controles inline do Admin de Interações."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from django.contrib import admin as django_admin

from leads.admin import InteracaoAdmin
from leads.models import Interacao, Segmento, StatusFunil


def _interacao() -> SimpleNamespace:
    return SimpleNamespace(
        pk=uuid4(),
        prospect=SimpleNamespace(
            segmento=Segmento.SALAO,
            status_funil=StatusFunil.NOVO,
        ),
    )


def test_select_de_segmento_tem_rotulo_acessivel():
    painel = InteracaoAdmin(Interacao, django_admin.site)

    html = str(painel.segmento_atual(_interacao()))

    assert 'aria-label="Segmento"' in html


def test_select_de_status_tem_rotulo_acessivel():
    painel = InteracaoAdmin(Interacao, django_admin.site)

    html = str(painel.status_editavel(_interacao()))

    assert 'aria-label="Status"' in html
