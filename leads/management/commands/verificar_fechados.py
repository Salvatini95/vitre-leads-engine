"""Audita prospects já gravados quanto a fechamento na fonte.

    uv run python manage.py verificar_fechados
    uv run python manage.py verificar_fechados --consultar-api
    uv run python manage.py verificar_fechados --consultar-api --confirmar

Sem `--consultar-api` o comando não toca a rede: só relata o que o banco já
sabe. E o que o banco sabe sobre os 400 prospects de Maringá é NADA — eis a
limitação, que é o principal resultado deste comando:

    O filtro de fechados sempre existiu na captação (`_coletar` descarta quem
    vem com `date_closed`), mas o descarte era silencioso: o candidato sumia
    antes de virar linha no banco e nada registrava que ele existiu. Não há
    campo histórico a reler. Para os 400 já captados, "quantos estavam
    fechados" não é uma pergunta respondível offline.

Daí `--consultar-api`, que é opt-in e custa franquia: 1 requisição por
prospect no endpoint de detalhes. Ele NÃO recaptura — não busca lugar novo,
não cria prospect, não mexe em curadoria. Só pergunta "este id fechou?" para
ids que já estão no banco, então não há risco de duplicar.

Sem `--confirmar`, `--consultar-api` apenas mostra o custo e para. É a mesma
trava de custo do `captar`: nenhuma chamada paga sai daqui por acidente.

O que uma resposta vazia significa: a Foursquare não afirma que fechou. NÃO
significa que está aberto — a base deles quase não preenche `date_closed`
para pequeno negócio no Brasil, e foi justamente por isso que um salão
fechado atravessou o filtro e chegou à fila de verificação. O filtro está
certo; a fonte é que não sabe. Confirmação de fechamento continua sendo
trabalho da revisão manual.
"""

from __future__ import annotations

import asyncio
from collections import Counter

import httpx
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from leads.models import (
    Descarte,
    MotivoDescarte,
    Origem,
    Prospect,
    Segmento,
    StatusFunil,
    StatusVarredura,
    Varredura,
)
from leads.services.captacao import consumo_do_mes, saldo_da_franquia, teto_da_franquia
from leads.sources import criar_fonte

_FONTE = "foursquare"


class Command(BaseCommand):
    help = "Recheca fechamento dos prospects já captados (Foursquare)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--consultar-api",
            action="store_true",
            help="Consulta a API por id. Custa 1 requisição de franquia por prospect.",
        )
        parser.add_argument(
            "--confirmar",
            action="store_true",
            help="Autoriza o gasto de franquia. Sem isto, --consultar-api só estima.",
        )
        parser.add_argument(
            "--limite",
            type=int,
            help="Consulta no máximo N prospects. Útil para amostrar antes de gastar.",
        )

    def handle(self, *args, **opts):
        pendentes = Prospect.objects.filter(
            origem=Origem.FOURSQUARE,
            fechado_na_fonte="",
        ).exclude(status_funil=StatusFunil.DESCARTADO)

        ja_marcados = Prospect.objects.exclude(fechado_na_fonte="").count()
        total = pendentes.count()

        self.stdout.write(f"Prospects Foursquare sem recheca: {total}")
        self.stdout.write(f"Já marcados como fechados na fonte: {ja_marcados}")

        if not opts["consultar_api"]:
            self._explicar_limite_offline(total)
            return

        if opts["limite"]:
            pendentes = pendentes[: opts["limite"]]
            total = pendentes.count()

        saldo = saldo_da_franquia(_FONTE)
        self.stdout.write(
            f"Franquia ({_FONTE}): {consumo_do_mes(_FONTE)}/{teto_da_franquia(_FONTE)} "
            f"usada, saldo {saldo}. Custo desta recheca: {total} requisições."
        )

        if total > saldo:
            raise CommandError(
                f"Recheca exigiria {total} requisições e o saldo é {saldo}. "
                "Use --limite para amostrar."
            )

        if not opts["confirmar"]:
            self.stdout.write(
                self.style.WARNING(
                    "Nada foi chamado. Repita com --confirmar para gastar a franquia."
                )
            )
            return

        alvos = list(pendentes.values_list("id", "origem_id", "nome"))
        resultados = asyncio.run(self._rechecar(alvos))
        self._aplicar(resultados, alvos)

    def _explicar_limite_offline(self, total: int) -> None:
        self.stdout.write(
            self.style.WARNING(
                "\nNão há como saber retroativamente, só com o banco, quantos dos "
                "prospects já captados estavam fechados."
            )
        )
        self.stdout.write(
            "Motivo: o descarte por fechamento acontece na coleta, antes da "
            "gravação. O candidato fechado nunca virou linha, e nenhuma coluna "
            "guardou o `date_closed` de quem passou. Não existe histórico a reler.\n"
        )
        self.stdout.write(
            "Captação NOVA já fica registrada: `Varredura.total_fechados` conta "
            "os descartados por fechamento, separado de dedup e de banimento."
        )
        self.stdout.write(
            f"\nPara responder sobre os {total} já gravados é preciso perguntar à "
            "API, 1 requisição por prospect:"
        )
        self.stdout.write("  manage.py verificar_fechados --consultar-api")
        self.stdout.write(
            "\nAtenção ao que isso responde: `date_closed` vazio é a Foursquare "
            "não afirmar nada, não é confirmação de que o negócio está aberto. "
            "A fila de verificação manual continua sendo o que resolve."
        )

    async def _rechecar(
        self, alvos: list[tuple[str, str, str]]
    ) -> dict[str, str | None]:
        """Devolve {origem_id: evidência}. `None` marca id que a base não conhece.

        Sequencial de propósito: são centenas de chamadas contra uma franquia
        compartilhada, e disparar tudo em paralelo é o caminho mais curto para
        um 429 que derruba a recheca inteira no meio.
        """
        achados: dict[str, str | None] = {}

        async with criar_fonte(_FONTE) as cliente:
            for _, origem_id, nome in alvos:
                try:
                    achados[origem_id] = await cliente.consultar_fechamento(origem_id)
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 404:
                        # Sumiu da base. É indício de fechamento, não prova:
                        # a Foursquare também remove duplicata e registro
                        # ruim. Fica registrado como indício e vai para
                        # revisão humana, não para descarte automático.
                        achados[origem_id] = None
                        continue
                    self.stderr.write(
                        f"Erro ao consultar {nome} ({origem_id}): "
                        f"HTTP {exc.response.status_code}. Recheca interrompida."
                    )
                    break
                except httpx.HTTPError as exc:
                    self.stderr.write(
                        f"Falha de rede em {nome}: {exc}. Recheca interrompida."
                    )
                    break

        return achados

    def _aplicar(
        self,
        resultados: dict[str, str | None],
        alvos: list[tuple[str, str, str]],
    ) -> None:
        por_id = {origem_id: pk for pk, origem_id, _ in alvos}
        contagem: Counter[str] = Counter()

        for origem_id, evidencia in resultados.items():
            contagem["consultados"] += 1

            if evidencia is None:
                contagem["sumiu_da_base"] += 1
                Prospect.objects.filter(pk=por_id[origem_id]).update(
                    fechado_na_fonte="ausente da base (404)",
                    atualizado_em=timezone.now(),
                )
                continue

            if not evidencia:
                contagem["sem_marca_de_fechamento"] += 1
                continue

            contagem["fechados"] += 1
            prospect = Prospect.objects.get(pk=por_id[origem_id])
            prospect.fechado_na_fonte = evidencia
            prospect.status_funil = StatusFunil.DESCARTADO
            prospect.ativo_no_funil = False
            prospect.save()

            # `banido=False`: fechamento é reversível — negócio reabre, e a
            # Foursquare corrige registro errado. Banir aqui vetaria o lead
            # para sempre por causa de um dado que pode mudar amanhã.
            Descarte.objects.create(
                prospect=prospect,
                motivo=MotivoDescarte.FECHADO,
                observacao=f"recheca automática na fonte: {evidencia}",
                banido=False,
            )

        self._registrar_custo(contagem["consultados"])

        self.stdout.write(
            self.style.SUCCESS(
                f"\n{contagem['consultados']} consultado(s): "
                f"{contagem['fechados']} fechados (descartados), "
                f"{contagem['sumiu_da_base']} sumiram da base (marcados, mantidos "
                f"na fila), {contagem['sem_marca_de_fechamento']} sem marca."
            )
        )
        self.stdout.write(
            "Lembrete: 'sem marca' não é confirmação de que está aberto — "
            "a Foursquare raramente preenche esse campo no Brasil."
        )

    def _registrar_custo(self, requisicoes: int) -> None:
        """Grava o gasto como uma Varredura para a franquia continuar honesta.

        Sem isto, a recheca consumiria franquia real sem aparecer no contador
        do mês, e a trava de custo do `captar` passaria a proteger com base
        num número menor que o gasto verdadeiro.
        """
        if not requisicoes:
            return

        Varredura.objects.create(
            termo_busca="recheca de fechamento (verificar_fechados)",
            segmento=Segmento.OUTRO,
            fonte=Origem.FOURSQUARE,
            cidade=settings.CIDADE_PADRAO,
            estado=settings.ESTADO_PADRAO,
            status=StatusVarredura.CONCLUIDA,
            total_requisicoes=requisicoes,
            concluido_em=timezone.now(),
        )
