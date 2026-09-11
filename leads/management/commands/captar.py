"""Roda a captação de uma atividade sobre a grade de uma cidade.

    uv run python manage.py captar --segmento SALAO
    uv run python manage.py captar --segmento NAIL --quadrante Q3
    uv run python manage.py captar --segmento SALAO --dry-run
    uv run python manage.py captar --segmento SALAO --fonte foursquare
    uv run python manage.py captar --nicho motoboys --termo "motoboy"

Uma varredura por quadrante. A franquia é conferida antes de cada uma e é
contada por fonte. Uma busca já iniciada ainda pode consumir mais de uma
requisição; a limitação está documentada como dívida operacional.

Sem `--fonte`, roda no Google Places, que segue sendo a fonte primária.
"""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from leads.models import Segmento
from leads.services.captacao_cidade import (
    CaptacaoCidadeError,
    CaptacaoCidadeFalhaFinalizacaoError,
    CaptacaoCidadeParcialError,
    ResultadoCaptacaoCidade,
    executar_captacao_cidade,
    planejar_captacao_cidade,
)
from leads.sources import FONTE_PADRAO, FONTES


class Command(BaseCommand):
    help = "Capta estabelecimentos sem site por cidade, quadrante a quadrante."

    def add_arguments(self, parser):
        parser.add_argument(
            "--segmento",
            choices=[s.value for s in Segmento],
            help=(
                "Segmento técnico. Obrigatório no modo legado; no modo com "
                "--nicho/--termo, o padrão é OUTRO."
            ),
        )
        parser.add_argument(
            "--nicho",
            help="Código exato de um Nicho existente e ativo.",
        )
        parser.add_argument(
            "--termo",
            help="Atividade buscada, sem cidade ou estado.",
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
        fonte = opts["fonte"]
        modo_novo = opts["nicho"] is not None or opts["termo"] is not None
        nicho_codigo, termo, segmento = self._contrato_canonico(opts)
        rotulo_segmento = Segmento(segmento).label

        try:
            plano = planejar_captacao_cidade(
                nicho_codigo=nicho_codigo,
                termo=termo,
                segmento=segmento,
                cidade=cidade,
                estado=estado,
                fonte=fonte,
                quadrante_rotulo=opts["quadrante"],
            )
        except CaptacaoCidadeError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            f"Segmento: {rotulo_segmento} | {cidade}/{estado} | "
            f"{len(plano.quadrantes)} quadrante(s) | fonte: {fonte}"
        )
        if modo_novo:
            self.stdout.write(
                f"Nicho: {plano.nicho.nome} ({plano.nicho.codigo}) | "
                f"Consulta: {plano.consulta}"
            )
        self.stdout.write(
            f"Franquia ({fonte}): {plano.consumo_franquia}/{plano.teto_franquia} "
            f"usada, saldo {plano.saldo_franquia}. "
            f"Custo máximo desta captação: {plano.estimativa_requisicoes} requisições."
        )

        if opts["dry_run"]:
            self.stdout.write(self.style.WARNING("dry-run — nada foi chamado."))
            return

        if plano.estimativa_requisicoes > plano.saldo_franquia:
            self.stdout.write(
                self.style.WARNING(
                    "Atenção: o pior caso excede o saldo. O saldo será reavaliado "
                    "antes de cada quadrante; uma busca iniciada pode consumir "
                    "mais de uma requisição."
                )
            )

        try:
            resultado = executar_captacao_cidade(plano)
        except CaptacaoCidadeFalhaFinalizacaoError as exc:
            self._imprimir_varreduras(exc.resultado)
            raise CommandError(str(exc)) from exc
        except CaptacaoCidadeParcialError as exc:
            self._imprimir_varreduras(exc.resultado)
            raise CommandError(str(exc)) from exc
        except CaptacaoCidadeError as exc:
            raise CommandError(str(exc)) from exc
        except ValueError as exc:
            # Erro operacional não previsto pelo contrato de domínio.
            raise CommandError(str(exc)) from exc

        self._imprimir_varreduras(resultado)

        if resultado.franquia_esgotada:
            self.stdout.write(self.style.ERROR(resultado.franquia_esgotada))

        self.stdout.write(
            self.style.SUCCESS(
                f"\nTotal: {resultado.total_encontrados} estabelecimentos, "
                f"{resultado.total_sem_site} sem site, "
                f"{resultado.total_novos} prospects novos. "
                f"Custo: {resultado.total_requisicoes} requisições."
            )
        )
        if resultado.total_fechados:
            self.stdout.write(
                f"{resultado.total_fechados} descartado(s) por estarem fechados na fonte "
                "(não entraram na fila de verificação)."
            )
        self.stdout.write("Próximo passo: curadoria no Admin (status NOVO).")

    def _imprimir_varreduras(self, resultado: ResultadoCaptacaoCidade) -> None:
        for varredura in resultado.varreduras:
            if varredura == resultado.varredura_com_erro:
                self.stdout.write(
                    f"  {varredura.quadrante.rotulo}: ERRO após "
                    f"{varredura.total_requisicoes} req — {varredura.erro}"
                )
                continue

            fechados = (
                f", {varredura.total_fechados} fechados descartados"
                if varredura.total_fechados
                else ""
            )
            self.stdout.write(
                f"  {varredura.quadrante.rotulo}: {varredura.total_encontrados} achados, "
                f"{varredura.total_sem_site} sem site, {varredura.total_novos} novos"
                f"{fechados} ({varredura.total_requisicoes} req)"
            )

    @staticmethod
    def _contrato_canonico(opts) -> tuple[str, str, str]:
        nicho = opts["nicho"]
        termo = opts["termo"]

        if (nicho is None) != (termo is None):
            raise CommandError("--nicho e --termo devem ser informados juntos.")

        if nicho is not None:
            return nicho, termo, opts["segmento"] or Segmento.OUTRO

        segmento = opts["segmento"]
        if segmento is None:
            raise CommandError(
                "Informe --segmento no modo legado ou --nicho e --termo no modo novo."
            )

        return "beleza", Segmento(segmento).label, segmento
