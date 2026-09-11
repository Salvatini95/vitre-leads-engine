"""Aplica retroativamente a regra nova aos prospects já captados da Foursquare.

Contexto: os 400 prospects de Maringá foram gravados sob a premissa herdada
do Google — "campo website vazio = não tem site". Para a Foursquare essa
premissa é falsa: 373 dos 400 vieram com o campo vazio porque a fonte não tem
o dado, não porque o negócio não tenha site.

O que esta migration faz, e o que deliberadamente NÃO faz:

- Move os prospects da Foursquare para VERIFICAR_SITE.
- Zera `tem_site_real` para NULL SOMENTE onde nada foi verificado (evidência
  "sem website"). Nos 27 em que o validador seguiu uma URL de verdade — DNS
  morto, 404, conteúdo vazio, perfil de terceiro — o `False` é fato apurado e
  fica registrado; esses vão para a fila junto, mas com a evidência
  preservada, e a tag os distingue.
- Não toca em `GOOGLE_PLACES` nem em nenhuma outra origem.
- Não toca em quem já foi revisado à mão (`revisado_manualmente=True`):
  curadoria humana é soberana e não é desfeita por migration.
- Não cria, não duplica e não recaptura nada — é UPDATE puro.

A reversão devolve os prospects a NOVO com `tem_site_real=False`, que é o
estado exato em que estavam antes.
"""

from django.db import migrations

ORIGEM_FOURSQUARE = "FOURSQUARE"
EVIDENCIA_SEM_URL = "sem website"
STATUS_VERIFICAR = "VERIFICAR_SITE"
STATUS_NOVO = "NOVO"


def aplicar(apps, schema_editor):
    Prospect = apps.get_model("leads", "Prospect")

    pendentes = Prospect.objects.filter(
        origem=ORIGEM_FOURSQUARE,
        revisado_manualmente=False,
    )

    # Primeiro o NULL, depois o status: assim a ordem não depende de o
    # filtro por status já ter mudado.
    pendentes.filter(site_evidencia=EVIDENCIA_SEM_URL).update(tem_site_real=None)
    pendentes.update(status_funil=STATUS_VERIFICAR, ativo_no_funil=False)


def reverter(apps, schema_editor):
    Prospect = apps.get_model("leads", "Prospect")

    voltando = Prospect.objects.filter(
        origem=ORIGEM_FOURSQUARE,
        status_funil=STATUS_VERIFICAR,
    )
    voltando.filter(tem_site_real=None).update(tem_site_real=False)
    voltando.update(status_funil=STATUS_NOVO, ativo_no_funil=False)


class Migration(migrations.Migration):
    dependencies = [
        ("leads", "0004_prospectverificacao_alter_prospect_status_funil"),
    ]

    operations = [
        migrations.RunPython(aplicar, reverter),
    ]
