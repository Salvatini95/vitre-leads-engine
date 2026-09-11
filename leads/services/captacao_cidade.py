"""Caso de uso compartilhado para planejar e executar captação por cidade."""

from __future__ import annotations

from dataclasses import dataclass

from leads.models import Nicho, Quadrante, Segmento, Varredura
from leads.services.captacao import (
    FalhaFinalizacaoVarreduraError,
    FranquiaEsgotadaError,
    VarreduraParcialError,
    consumo_do_mes,
    executar_varredura,
    teto_da_franquia,
)
from leads.sources import (
    FONTE_PADRAO,
    ConfiguracaoFonteError,
    classe_da_fonte,
    validar_configuracao_fonte,
)
from leads.sources.models import BuscaParcialError


class CaptacaoCidadeError(Exception):
    """Plano de captação inválido para o domínio ou para o estado atual."""


@dataclass(frozen=True, slots=True)
class PlanoCaptacaoCidade:
    """Entrada e operação estimada, prontas para execução imediata.

    O planejamento valida Nicho, termo, Segmento, fonte, grade, quadrantes,
    estimativa e saldo sem exigir credencial. A execução faz o preflight local
    da fonte. Um futuro uso adiado deve revalidar Nicho, quadrantes e saldo.
    """

    nicho: Nicho
    termo: str
    consulta: str
    segmento: str
    cidade: str
    estado: str
    fonte: str
    quadrantes: tuple[Quadrante, ...]
    estimativa_requisicoes: int
    consumo_franquia: int
    teto_franquia: int
    saldo_franquia: int


@dataclass(frozen=True, slots=True)
class ResultadoCaptacaoCidade:
    """Varreduras registradas e totais de uma execução do plano."""

    plano: PlanoCaptacaoCidade
    varreduras: tuple[Varredura, ...]
    total_requisicoes: int
    total_encontrados: int
    total_sem_site: int
    total_novos: int
    total_fechados: int
    franquia_esgotada: str = ""
    varredura_com_erro: Varredura | None = None


class CaptacaoCidadeParcialError(CaptacaoCidadeError):
    """Falha parcial com progresso agregado e causa original acessíveis."""

    def __init__(
        self,
        *,
        resultado: ResultadoCaptacaoCidade,
        causa: BuscaParcialError,
        quadrante: Quadrante | None,
    ) -> None:
        self.resultado = resultado
        self.causa = causa
        self.quadrante = quadrante
        self.varredura = resultado.varredura_com_erro
        rotulo = quadrante.rotulo if quadrante else "sem quadrante"
        super().__init__(f"Captação interrompida em {rotulo}: {causa}")


class CaptacaoCidadeFalhaFinalizacaoError(CaptacaoCidadeError):
    """Falha pública quando a Varredura não pôde ser encerrada no banco."""

    def __init__(
        self,
        *,
        resultado: ResultadoCaptacaoCidade,
        varredura: Varredura,
        causa_operacional: Exception,
        causa_finalizacao: Exception,
        quadrante: Quadrante | None,
        busca_parcial: bool,
    ) -> None:
        self.resultado = resultado
        self.varredura = varredura
        self.causa_operacional = causa_operacional
        self.causa_finalizacao = causa_finalizacao
        self.quadrante = quadrante
        self.busca_parcial = busca_parcial
        self.estado_erro_persistido = False
        rotulo = quadrante.rotulo if quadrante else "sem quadrante"
        super().__init__(
            f"Captação interrompida em {rotulo}: não foi possível registrar "
            "o estado de erro da Varredura."
        )


def planejar_captacao_cidade(
    *,
    nicho_codigo: str,
    termo: str,
    segmento: str,
    cidade: str,
    estado: str,
    fonte: str = FONTE_PADRAO,
    quadrante_rotulo: str | None = None,
) -> PlanoCaptacaoCidade:
    """Planeja e estima sem exigir credencial, criar Varredura ou realizar HTTP."""
    termo = termo.strip()
    if not termo:
        raise CaptacaoCidadeError("O termo da atividade não pode ser vazio.")

    try:
        segmento = Segmento(segmento).value
    except ValueError:
        raise CaptacaoCidadeError(f"Segmento inválido: {segmento!r}.") from None

    try:
        classe_fonte = classe_da_fonte(fonte)
    except ValueError as exc:
        raise CaptacaoCidadeError(str(exc)) from exc

    try:
        nicho = Nicho.objects.get(codigo=nicho_codigo)
    except Nicho.DoesNotExist:
        raise CaptacaoCidadeError(
            f"Nicho '{nicho_codigo}' não existe. Cadastre-o no Admin antes de captar."
        ) from None

    if not nicho.ativo:
        raise CaptacaoCidadeError(
            f"Nicho '{nicho_codigo}' está inativo e não pode receber novas captações."
        )

    quadrantes = Quadrante.objects.filter(cidade=cidade, estado=estado)
    if quadrante_rotulo:
        quadrantes = quadrantes.filter(rotulo=quadrante_rotulo)

    quadrantes = tuple(quadrantes)
    if not quadrantes:
        if quadrante_rotulo:
            raise CaptacaoCidadeError(
                f"Quadrante '{quadrante_rotulo}' não existe para {cidade}/{estado}."
            )
        raise CaptacaoCidadeError(
            f"Nenhum quadrante para {cidade}/{estado}. "
            f"Rode antes: manage.py gerar_grade --cidade {cidade} --estado {estado}"
        )

    consulta = f"{termo} em {cidade} {estado}"
    estimativa = len(quadrantes) * classe_fonte.MAX_REQUISICOES_POR_BUSCA
    consumo = consumo_do_mes(fonte)
    teto = teto_da_franquia(fonte)

    return PlanoCaptacaoCidade(
        nicho=nicho,
        termo=termo,
        consulta=consulta,
        segmento=segmento,
        cidade=cidade,
        estado=estado,
        fonte=fonte,
        quadrantes=quadrantes,
        estimativa_requisicoes=estimativa,
        consumo_franquia=consumo,
        teto_franquia=teto,
        saldo_franquia=max(0, teto - consumo),
    )


def executar_captacao_cidade(
    plano: PlanoCaptacaoCidade,
) -> ResultadoCaptacaoCidade:
    """Executa uma Varredura por quadrante e interrompe se a franquia acabar."""
    try:
        validar_configuracao_fonte(plano.fonte)
    except ConfiguracaoFonteError as exc:
        raise CaptacaoCidadeError(
            f"Configuração inválida para {plano.fonte}: {exc}"
        ) from exc

    varreduras: list[Varredura] = []
    franquia_esgotada = ""

    for quadrante in plano.quadrantes:
        try:
            varredura = executar_varredura(
                nicho=plano.nicho,
                segmento=plano.segmento,
                consulta=plano.consulta,
                cidade=plano.cidade,
                estado=plano.estado,
                quadrante=quadrante,
                fonte=plano.fonte,
            )
        except FranquiaEsgotadaError as exc:
            franquia_esgotada = str(exc)
            break
        except FalhaFinalizacaoVarreduraError as exc:
            resultado = _agregar_resultado(plano, varreduras)
            raise CaptacaoCidadeFalhaFinalizacaoError(
                resultado=resultado,
                varredura=exc.varredura,
                causa_operacional=exc.causa_operacional,
                causa_finalizacao=exc.causa_finalizacao,
                quadrante=quadrante,
                busca_parcial=exc.busca_parcial,
            ) from exc
        except VarreduraParcialError as exc:
            varreduras.append(exc.varredura)
            resultado = _agregar_resultado(
                plano,
                varreduras,
                varredura_com_erro=exc.varredura,
            )
            raise CaptacaoCidadeParcialError(
                resultado=resultado,
                causa=exc.causa,
                quadrante=quadrante,
            ) from exc

        varreduras.append(varredura)

    return _agregar_resultado(
        plano,
        varreduras,
        franquia_esgotada=franquia_esgotada,
    )


def _agregar_resultado(
    plano: PlanoCaptacaoCidade,
    varreduras: list[Varredura],
    *,
    franquia_esgotada: str = "",
    varredura_com_erro: Varredura | None = None,
) -> ResultadoCaptacaoCidade:
    """Materializa os totais do progresso efetivamente persistido."""
    return ResultadoCaptacaoCidade(
        plano=plano,
        varreduras=tuple(varreduras),
        total_requisicoes=sum(v.total_requisicoes for v in varreduras),
        total_encontrados=sum(v.total_encontrados for v in varreduras),
        total_sem_site=sum(v.total_sem_site for v in varreduras),
        total_novos=sum(v.total_novos for v in varreduras),
        total_fechados=sum(v.total_fechados for v in varreduras),
        franquia_esgotada=franquia_esgotada,
        varredura_com_erro=varredura_com_erro,
    )
