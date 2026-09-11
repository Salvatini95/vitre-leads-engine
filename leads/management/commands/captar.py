"""Roda a captação de um segmento sobre a grade de uma cidade.

    uv run python manage.py captar --segmento SALAO
    uv run python manage.py captar --segmento NAIL --quadrante Q3
    uv run python manage.py captar --segmento SALAO --dry-run
    uv run python manage.py captar --segmento SALAO --fonte foursquare

Uma varredura por quadrante. A franquia é conferida antes de cada uma — e é
por fonte, cada API tem teto próprio — então a captação para sozinha ao bater
o teto e nunca gera fatura por acidente.

Sem `--fonte`, roda no Google Places, que segue sendo a fonte primária.
"""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from leads.models import Quadrante, Segmento
from leads.services.captacao import (
    FranquiaEsgotadaError,
    consumo_do_mes,
    executar_varredura,
    saldo_da_franquia,
    teto_da_franquia,
)
from leads.sources import FONTE_PADRAO, FONTES, classe_da_fonte


class Command(BaseCommand):
    help = "Capta estabelecimentos sem site de um segmento, quadrante a quadrante."

    def add_arguments(self, parser):
        parser.add_argument(
            "--segmento",
            required=True,
            choices=[s.value for s in Segmento],
            help="Segmento-alvo. Vira o termo de busca enviado à API.",
        )
        parser.add_argument("--cidade", default=settings.CIDADE_PADRAO)
        parser.add_argument("--estado", default=settings.ESTADO_PADRAO)
        parser.add_argument(
            "--quadrante",
            help="Rótulo (ex: Q3). Omitido = varre a grade inteira.",
        )
        parser.add_argument(
            "--fonte",
            default=FONTE_PADRAO,
            choices=sorted(FONTES),
            help=(
                f"Fonte de dados. Padrão: {FONTE_PADRAO}. "
                "'foursquare' é contingência — cobertura menor no Brasil."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Só mostra o plano e o custo estimado. Não chama a API.",
        )

    def handle(self, *args, **opts):
        cidade, estado = opts["cidade"], opts["estado"]
        segmento = opts["segmento"]
        fonte = opts["fonte"]
        rotulo_segmento = Segmento(segmento).label

        quadrantes = Quadrante.objects.filter(cidade=cidade, estado=estado)
        if opts["quadrante"]:
            quadrantes = quadrantes.filter(rotulo=opts["quadrante"])

        quadrantes = list(quadrantes)
        if not quadrantes:
            raise CommandError(
                f"Nenhum quadrante para {cidade}/{estado}. "
                f"Rode antes: manage.py gerar_grade --cidade {cidade} --estado {estado}"
            )

        # Pior caso: o teto de páginas da fonte, por quadrante.
        classe_fonte = classe_da_fonte(fonte)
        estimativa = len(quadrantes) * classe_fonte.MAX_REQUISICOES_POR_BUSCA
        saldo = saldo_da_franquia(fonte)

        self.stdout.write(
            f"Segmento: {rotulo_segmento} | {cidade}/{estado} | "
            f"{len(quadrantes)} quadrante(s) | fonte: {fonte}"
        )
        self.stdout.write(
            f"Franquia ({fonte}): {consumo_do_mes(fonte)}/{teto_da_franquia(fonte)} "
            f"usada, saldo {saldo}. "
            f"Custo máximo desta captação: {estimativa} requisições."
        )

        if opts["dry_run"]:
            self.stdout.write(self.style.WARNING("dry-run — nada foi chamado."))
            return

        if estimativa > saldo:
            self.stdout.write(
                self.style.WARNING(
                    "Atenção: o pior caso excede o saldo. A captação vai rodar até "
                    "o teto e parar sozinha, sem gerar cobrança."
                )
            )

        totais = {"req": 0, "encontrados": 0, "sem_site": 0, "novos": 0}

        for quadrante in quadrantes:
            try:
                varredura = executar_varredura(
                    segmento=segmento,
                    segmento_rotulo=rotulo_segmento,
                    cidade=cidade,
                    estado=estado,
                    quadrante=quadrante,
                    fonte=fonte,
                )
            except FranquiaEsgotadaError as exc:
                self.stdout.write(self.style.ERROR(str(exc)))
                break
            except ValueError as exc:
                # Chave de API ausente no .env — erro de operador, não de
                # execução. Aborta sem stack trace.
                raise CommandError(str(exc)) from exc

            totais["req"] += varredura.total_requisicoes
            totais["encontrados"] += varredura.total_encontrados
            totais["sem_site"] += varredura.total_sem_site
            totais["novos"] += varredura.total_novos

            self.stdout.write(
                f"  {quadrante.rotulo}: {varredura.total_encontrados} achados, "
                f"{varredura.total_sem_site} sem site, {varredura.total_novos} novos "
                f"({varredura.total_requisicoes} req)"
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"\nTotal: {totais['encontrados']} estabelecimentos, "
                f"{totais['sem_site']} sem site, {totais['novos']} prospects novos. "
                f"Custo: {totais['req']} requisições."
            )
        )
        self.stdout.write("Próximo passo: curadoria no Admin (status NOVO).")
