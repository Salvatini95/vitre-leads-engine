"""Normaliza e classifica os telefones já gravados.

Sem esta migração, os 400 prospects captados antes de `telefone_tipo` existir
ficariam todos em VAZIO e a fila de verificação ordenaria como se ninguém
tivesse telefone — pior que o comportamento anterior, não melhor.

Efeito medido no banco de Maringá: dos 158 com o campo preenchido, 50 são
celular de formato válido, 101 são fixo, 7 não casam com formato nenhum
(4 celulares antigos sem o 9º dígito e 3 números sem DDD). A fila prioriza
50, não 158.

Reversível: a volta reescreve o telefone no formato humano `(DD) NNNNN-NNNN`
e zera o tipo. Não é o texto byte a byte que a Foursquare mandou — esse dado
não fica guardado em lugar nenhum —, mas é o mesmo número, legível, que é o
que a tela precisa.
"""

from __future__ import annotations

from django.db import migrations

from leads.utils.telefone import TipoTelefone, analisar

# Lotes para não carregar a tabela inteira em memória quando ela crescer.
_LOTE = 500


def normalizar(apps, schema_editor):
    Prospect = apps.get_model("leads", "Prospect")

    alterados = []
    for prospect in Prospect.objects.exclude(telefone="").iterator(chunk_size=_LOTE):
        telefone = analisar(prospect.telefone)
        prospect.telefone = telefone.e164 or prospect.telefone
        prospect.telefone_tipo = telefone.tipo
        alterados.append(prospect)

    Prospect.objects.bulk_update(
        alterados, ["telefone", "telefone_tipo"], batch_size=_LOTE
    )


def desnormalizar(apps, schema_editor):
    Prospect = apps.get_model("leads", "Prospect")

    alterados = []
    for prospect in Prospect.objects.exclude(telefone="").iterator(chunk_size=_LOTE):
        prospect.telefone = _formato_humano(prospect.telefone)
        prospect.telefone_tipo = TipoTelefone.VAZIO
        alterados.append(prospect)

    Prospect.objects.bulk_update(
        alterados, ["telefone", "telefone_tipo"], batch_size=_LOTE
    )


def _formato_humano(e164: str) -> str:
    """`+554499120926` → `(44) 99912-0926`. Devolve a entrada se não casar."""
    if not e164.startswith("+55"):
        return e164

    nacional = e164[3:]
    if len(nacional) not in (10, 11):
        return e164

    ddd, assinante = nacional[:2], nacional[2:]
    corte = len(assinante) - 4
    return f"({ddd}) {assinante[:corte]}-{assinante[corte:]}"


class Migration(migrations.Migration):
    dependencies = [
        ("leads", "0006_prospect_fechado_na_fonte_prospect_telefone_tipo_and_more"),
    ]

    operations = [
        migrations.RunPython(normalizar, desnormalizar),
    ]
