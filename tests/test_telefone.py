"""Testes da validação de telefone.

O caso que motivou o módulo: a fila contava 158 "com telefone" olhando só se
o campo estava preenchido, e 101 daqueles 158 eram fixos. Os exemplos abaixo
são strings reais do banco de Maringá, não inventadas.

A regra que nenhum teste pode deixar cair: `e_celular` fala sobre o FORMATO da
string. Não existe teste aqui afirmando que um número tem WhatsApp, porque o
código não tem como saber isso sem contatar a linha.
"""

from __future__ import annotations

import pytest

from leads.utils.telefone import (
    TipoTelefone,
    analisar,
    e_celular_valido,
    normalizar,
)


@pytest.mark.parametrize(
    "bruto",
    [
        "(44) 99912-0926",  # como a Foursquare devolve
        "44999120926",
        "+55 44 99912-0926",
        "+5544999120926",
        "0055 44 99912-0926",  # prefixo internacional + código do país
        "44 9 9912 0926",
        "(44)99912-0926",
        "044 99912-0926",  # tronco interurbano antigo
    ],
)
def test_variacoes_de_formatacao_do_mesmo_celular(bruto):
    """Formatação não muda o número: tudo converge para o mesmo E.164."""
    telefone = analisar(bruto)

    assert telefone.tipo is TipoTelefone.CELULAR
    assert telefone.e164 == "+5544999120926"
    assert telefone.e_celular


@pytest.mark.parametrize(
    ("bruto", "esperado"),
    [
        ("(44) 3244-6413", "+554432446413"),
        ("(44) 3031-6804", "+554430316804"),
    ],
)
def test_fixo_normaliza_mas_nao_e_celular(bruto, esperado):
    """Fixo é número válido — só não serve para abordagem por mensagem."""
    telefone = analisar(bruto)

    assert telefone.tipo is TipoTelefone.FIXO
    assert telefone.e164 == esperado
    assert not telefone.e_celular


@pytest.mark.parametrize("bruto", ["4499050878", "4499928072", "4488022525"])
def test_celular_antigo_nao_vira_celular_valido(bruto):
    """Sem o 9º dígito não é celular atual — e o 9 NÃO é inserido à força.

    Inventar o dígito produziria um número que disca para outra pessoa. O
    lugar disso é a revisão manual, então fica LEGADO e fora da contagem.
    """
    telefone = analisar(bruto)

    assert telefone.tipo is TipoTelefone.LEGADO
    assert not telefone.e_celular
    # Normalizado mesmo assim: continua sendo um número discável com DDD.
    assert telefone.e164 == f"+55{bruto}"


@pytest.mark.parametrize("bruto", ["3266-2772", "3042-0227", "3301-9149"])
def test_numero_sem_ddd_e_invalido(bruto):
    """Sem DDD não há como discar de fora nem como deduzir o DDD sem inventar."""
    telefone = analisar(bruto)

    assert telefone.tipo is TipoTelefone.INVALIDO
    assert telefone.e164 == ""
    assert not telefone.e_celular


@pytest.mark.parametrize("bruto", ["", "   ", None])
def test_vazio(bruto):
    telefone = analisar(bruto)

    assert telefone.tipo is TipoTelefone.VAZIO
    assert not telefone.e_celular


@pytest.mark.parametrize("ddd", ["10", "00"])
def test_ddd_fora_da_faixa(ddd):
    assert analisar(f"{ddd}999120926").tipo is TipoTelefone.INVALIDO


def test_zero_na_frente_e_tronco_e_nao_ddd():
    """`019...` é tronco 0 + DDD 19, não um DDD 01 inválido.

    Documenta a leitura escolhida: o zero à esquerda de um número de 11 ou 12
    dígitos é discagem interurbana antiga, não parte do DDD.
    """
    telefone = analisar("01999120926")

    assert telefone.e164 == "+551999120926"
    # 8 dígitos começando em 9: celular do formato antigo, não celular atual.
    assert telefone.tipo is TipoTelefone.LEGADO


def test_ddd_55_nao_e_confundido_com_codigo_do_pais():
    """DDD 55 (RS) tem 11 dígitos e não pode ser cortado como se fosse o +55.

    Cortar aqui devolveria um número de 9 dígitos e o DDD do Rio Grande do Sul
    inteiro viraria lixo na base — falha silenciosa, número plausível e errado.
    """
    telefone = analisar("55999120926")

    assert telefone.tipo is TipoTelefone.CELULAR
    assert telefone.e164 == "+5555999120926"


def test_prefixo_do_pais_com_ddd_55():
    """`+55 55 9...` tem 13 dígitos: aí o corte do país é o certo."""
    assert analisar("+5555999120926").e164 == "+5555999120926"


def test_normalizar_preserva_o_que_nao_reconhece():
    """Número irreconhecível continua visível — descartá-lo apagaria dado."""
    assert normalizar("liga no salão") == "liga no salão"
    assert normalizar("") == ""


def test_normalizar_devolve_e164_quando_da():
    assert normalizar("(44) 99912-0926") == "+5544999120926"


def test_e_celular_valido_e_atalho_de_analisar():
    assert e_celular_valido("(44) 99912-0926")
    assert not e_celular_valido("(44) 3244-6413")
    assert not e_celular_valido("")


def test_ramal_ou_digito_extra_nao_passa_por_celular():
    """12 dígitos nacionais não é celular — é número com ramal ou erro."""
    assert analisar("449991209261").tipo is TipoTelefone.INVALIDO
