"""Validação e normalização de telefone brasileiro.

Existe porque a fila de verificação contava "158 com telefone" olhando apenas
se o campo estava vazio. Dos 158 captados em Maringá, 101 eram FIXO — número
que não abre conversa de WhatsApp e que, para o fluxo da VITRE (abordagem por
mensagem), vale quase o mesmo que campo vazio. A fila estava priorizando
manualmente 108 prospects por um sinal que não existia.

O que este módulo afirma e o que NÃO afirma:

- Afirma: "o texto tem FORMATO de celular brasileiro" — DDD plausível mais 9
  dígitos começando em 9.
- NÃO afirma: que a linha existe, que atende, ou que tem WhatsApp. Isso só se
  descobre contatando o número, o que o protocolo da VITRE proíbe fazer
  automaticamente. `e_celular` é um juízo sobre a STRING, nunca sobre a linha.

Sobre o nono dígito: números de 10 dígitos cujo assinante começa com 9 ou 8
(`4499050878`, `4488022525` nos dados de Maringá) são celulares do formato
antigo, anterior à migração de 2016. Não são promovidos a válidos aqui e o
9 NÃO é inserido à força: inventar dígito produz um número que disca para
outra pessoa. Ficam marcados como `LEGADO`, que é o que manda para revisão
manual em vez de descartar em silêncio.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

_SO_DIGITOS = re.compile(r"\D+")

# O contrato pedido é DDD de 11 a 99. É mais frouxo que a lista real da
# Anatel (não existe DDD 20, 23, 25...), e é frouxo de propósito: rejeitar um
# DDD válido custa um lead, aceitar um inválido custa uma linha na fila de
# revisão manual, que é onde o número ia parar de qualquer jeito.
_DDD_MIN = 11
_DDD_MAX = 99

_PREFIXO_PAIS = "55"

# Celular: 9 dígitos, o primeiro obrigatoriamente 9.
_CELULAR = re.compile(r"9\d{8}")
# Fixo: 8 dígitos, o primeiro de 2 a 5.
_FIXO = re.compile(r"[2-5]\d{7}")
# Celular pré-2016: 8 dígitos começando em 8 ou 9, sem o nono dígito.
_CELULAR_LEGADO = re.compile(r"[89]\d{7}")


class TipoTelefone(StrEnum):
    """Classificação do formato — não da linha."""

    CELULAR = "CELULAR"
    FIXO = "FIXO"
    LEGADO = "LEGADO"
    INVALIDO = "INVALIDO"
    VAZIO = "VAZIO"


# Rótulos para o `choices` do campo no modelo. Ficam aqui, junto do enum, para
# não haver uma segunda definição de tipo de telefone dentro de `models.py`
# que possa divergir desta em silêncio.
TIPO_TELEFONE_CHOICES = (
    (TipoTelefone.CELULAR, "Celular (formato válido)"),
    (TipoTelefone.FIXO, "Fixo"),
    (TipoTelefone.LEGADO, "Celular formato antigo (sem o 9º dígito)"),
    (TipoTelefone.INVALIDO, "Formato não reconhecido"),
    (TipoTelefone.VAZIO, "Sem telefone"),
)


@dataclass(frozen=True, slots=True)
class Telefone:
    """Um telefone depois de analisado.

    `e164` só é preenchido quando o número pôde ser normalizado com DDD —
    string vazia significa "não deu para normalizar", nunca um palpite.
    """

    original: str
    e164: str
    tipo: TipoTelefone

    @property
    def e_celular(self) -> bool:
        """True só para FORMATO de celular atual. Não diz nada sobre WhatsApp."""
        return self.tipo is TipoTelefone.CELULAR

    @property
    def normalizado(self) -> bool:
        return bool(self.e164)


def analisar(bruto: str | None) -> Telefone:
    """Classifica e normaliza um telefone como veio da fonte.

    Aceita as variações que as fontes devolvem: `(44) 99912-0926`,
    `4499120926`, `+55 44 99912-0926`, `44 9 9912 0926`.
    """
    original = (bruto or "").strip()
    if not original:
        return Telefone(original="", e164="", tipo=TipoTelefone.VAZIO)

    digitos = _SO_DIGITOS.sub("", original)

    # `+55...` e `0055...` viram o mesmo número nacional. O corte só acontece
    # se o que sobra ainda tiver tamanho de número com DDD — senão `55` era o
    # próprio DDD (Rio Grande do Sul) e cortá-lo mutilaria o número.
    if digitos.startswith("00"):
        digitos = digitos[2:]
    if digitos.startswith(_PREFIXO_PAIS) and len(digitos) in (12, 13):
        digitos = digitos[len(_PREFIXO_PAIS) :]

    # Tronco interurbano antigo (0xx) na frente do DDD.
    if len(digitos) in (11, 12) and digitos.startswith("0"):
        digitos = digitos[1:]

    if len(digitos) not in (10, 11):
        # Sem DDD (`3266-2772`, 8 dígitos) não há como discar de fora da
        # cidade nem como normalizar — o DDD não é dedutível do endereço sem
        # inventar dado.
        return Telefone(original=original, e164="", tipo=TipoTelefone.INVALIDO)

    ddd, assinante = digitos[:2], digitos[2:]

    if not (_DDD_MIN <= int(ddd) <= _DDD_MAX):
        return Telefone(original=original, e164="", tipo=TipoTelefone.INVALIDO)

    if _CELULAR.fullmatch(assinante):
        tipo = TipoTelefone.CELULAR
    elif _FIXO.fullmatch(assinante):
        tipo = TipoTelefone.FIXO
    elif _CELULAR_LEGADO.fullmatch(assinante):
        tipo = TipoTelefone.LEGADO
    else:
        return Telefone(original=original, e164="", tipo=TipoTelefone.INVALIDO)

    return Telefone(
        original=original,
        e164=f"+{_PREFIXO_PAIS}{ddd}{assinante}",
        tipo=tipo,
    )


def normalizar(bruto: str | None) -> str:
    """`+55DDNNNNNNNNN` quando dá para normalizar; senão o original limpo.

    Nunca devolve string vazia para entrada não vazia: um telefone que não
    casou com nenhum formato conhecido continua visível na tela de revisão,
    onde uma pessoa decide o que ele é. Descartá-lo aqui apagaria dado.
    """
    telefone = analisar(bruto)
    return telefone.e164 or telefone.original


def e_celular_valido(bruto: str | None) -> bool:
    """True quando o texto tem formato de celular BR atual.

    Formato apenas — não confirma que a linha existe nem que tem WhatsApp.
    """
    return analisar(bruto).e_celular
