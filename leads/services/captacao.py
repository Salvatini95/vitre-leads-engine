"""Orquestração de uma varredura: franquia → busca → filtro → persistência.

Divisão deliberada entre async e sync: a parte de rede (Places + validação de
sites) roda em asyncio, e a persistência roda em ORM síncrono normal. Assim
não há ORM assíncrono no meio de `asyncio.gather`, que é onde esse tipo de
código costuma virar armadilha.

Regras de negócio que vivem aqui:

- A franquia é conferida ANTES de disparar, e é POR FONTE: cada API tem teto
  próprio. Atingido o teto, a captação para.
- `total_requisicoes` é gravado mesmo quando a busca falha no meio.
- Recaptura não sobrescreve curadoria: prospect com `revisado_manualmente`
  mantém segmento e status.
- Estabelecimento que a fonte declara fechado não vira Prospect — é cortado
  na coleta, e contado à parte de dedup e de banimento.
- Telefone é normalizado para `+55DDNNNNNNNNN` e classificado por FORMATO na
  gravação (ver `leads.utils.telefone`).
- Prospect com descarte `banido` não volta ao funil.
- `termo_busca` guarda o termo da PRIMEIRA captura e não é sobrescrito.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from leads.filters.site_validator import SiteValidator, SiteVerdict
from leads.models import (
    Descarte,
    Nicho,
    OrigemLocalizacao,
    Prospect,
    Quadrante,
    StatusFunil,
    StatusVarredura,
    Varredura,
)
from leads.services.grade import CelulaGrade
from leads.sources import FONTE_PADRAO, classe_da_fonte, criar_fonte
from leads.sources.models import BuscaParcialError, ProspectCandidate
from leads.utils.telefone import analisar as analisar_telefone

logger = logging.getLogger(__name__)

# A franquia mensal da Google reseta à meia-noite do dia 1 no fuso do
# Pacífico, não no nosso. Contar por mês local adiantaria o reset e deixaria
# a trava de custo furada nos primeiros dias do mês. O mesmo fuso vale para a
# Foursquare: não sabemos o reset dela, e este é o mais conservador dos dois.
_FUSO_FRANQUIA = ZoneInfo("America/Los_Angeles")


class FranquiaEsgotadaError(Exception):
    """Teto mensal de requisições atingido — captação bloqueada."""


class VarreduraParcialError(Exception):
    """Falha parcial já persistida, com referência explícita à Varredura."""

    def __init__(self, *, varredura: Varredura, causa: BuscaParcialError) -> None:
        self.varredura = varredura
        self.causa = causa
        super().__init__(str(causa))


class FalhaFinalizacaoVarreduraError(Exception):
    """Falha ao persistir ERRO, preservando a operação e a finalização."""

    def __init__(
        self,
        *,
        varredura: Varredura,
        causa_operacional: Exception,
        causa_finalizacao: Exception,
    ) -> None:
        self.varredura = varredura
        self.causa_operacional = causa_operacional
        self.causa_finalizacao = causa_finalizacao
        self.busca_parcial = isinstance(causa_operacional, BuscaParcialError)
        self.estado_erro_persistido = False
        super().__init__(
            "Não foi possível persistir o encerramento da Varredura após uma falha."
        )


def _inicio_do_mes_da_franquia() -> datetime:
    """Início do mês corrente no fuso do Pacífico, em UTC."""
    agora = timezone.now().astimezone(_FUSO_FRANQUIA)
    primeiro = agora.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return primeiro.astimezone(ZoneInfo("UTC"))


def teto_da_franquia(fonte: str = FONTE_PADRAO) -> int:
    """Teto mensal de requisições DESTA fonte, lido do settings."""
    return getattr(settings, classe_da_fonte(fonte).SETTING_FRANQUIA)


def consumo_do_mes(fonte: str = FONTE_PADRAO) -> int:
    """Requisições já gastas nesta fonte no mês corrente da franquia.

    Filtra por fonte de propósito: varredura na Foursquare não pode descontar
    da franquia do Google, nem o contrário.
    """
    total = Varredura.objects.filter(
        criado_em__gte=_inicio_do_mes_da_franquia(),
        fonte=classe_da_fonte(fonte).ORIGEM,
    ).aggregate(total=Sum("total_requisicoes"))["total"]
    return total or 0


def saldo_da_franquia(fonte: str = FONTE_PADRAO) -> int:
    """Quantas requisições ainda cabem no mês para esta fonte."""
    return max(0, teto_da_franquia(fonte) - consumo_do_mes(fonte))


@dataclass(frozen=True, slots=True)
class Coleta:
    """Saída da parte de rede de uma varredura.

    `vereditos` vem na mesma ordem de `candidatos`. `fechados` são os que a
    fonte declarou fechados e que por isso NÃO estão em `candidatos` — é
    contagem de quem foi barrado, não de quem sobrou.
    """

    candidatos: list[ProspectCandidate]
    requisicoes: int
    vereditos: list[SiteVerdict]
    fechados: int = 0


async def _coletar(
    texto_query: str,
    celula: CelulaGrade | None,
    fonte: str = FONTE_PADRAO,
    segmento: str | None = None,
) -> Coleta:
    """Parte de rede: busca na fonte e valida o site de cada candidato.

    Estabelecimento que a fonte marca como fechado é descartado AQUI, antes
    da validação de site e antes de qualquer escrita: não entra na fila de
    verificação manual, porque não há tempo de revisor a gastar com um
    negócio que não existe mais. O corte é anterior ao dedup e ao banimento
    de propósito — os três motivos de sumiço ficam contados em separado.

    Em falha parcial da busca, a exceção preserva os candidatos coletados pela
    fonte e o custo consumido. Esta função não valida nem persiste esses
    candidatos parciais; o chamador registra somente o custo e o erro.

    A validação de site é idêntica para qualquer fonte: o critério comercial
    ("não tem site") não muda porque o endereço veio de outra API.
    """
    async with criar_fonte(fonte) as cliente:
        resultado = await cliente.buscar(texto_query, celula, segmento)

    candidatos = [c for c in resultado.candidatos if c.ativo]
    fechados = [c for c in resultado.candidatos if not c.ativo]

    for candidato in fechados:
        logger.info(
            "Descartado por fechamento na fonte: %s (%s) — %s",
            candidato.nome,
            candidato.origem_id,
            candidato.fechado_evidencia or "sem evidência declarada",
        )

    if fechados:
        logger.info(
            "%d de %d resultados descartados por estarem fechados na fonte %s",
            len(fechados),
            len(resultado.candidatos),
            fonte,
        )

    async with SiteValidator() as validador:
        vereditos = await asyncio.gather(
            *(validador.validar(c.website_url) for c in candidatos)
        )

    return Coleta(
        candidatos=candidatos,
        requisicoes=resultado.total_requisicoes,
        vereditos=list(vereditos),
        fechados=len(fechados),
    )


def _situacao_do_site(
    veredito: SiteVerdict,
    site_confiavel: bool,
) -> tuple[bool | None, str]:
    """Traduz o veredito em (tem_site_real, status_funil) conforme a fonte.

    Fonte confiável (Google): comportamento da Fase 1, intacto — `False`
    significa "não tem site" e o prospect entra direto na curadoria.

    Fonte não confiável (Foursquare): o prospect vai para VERIFICAR_SITE, e
    quando NADA foi verificado — a fonte não deu URL — `tem_site_real` vira
    `None`, que é o valor que o campo já reserva para "desconhecido".
    Gravar `False` ali seria o sistema afirmando um fato que ninguém apurou.
    """
    if site_confiavel:
        return veredito.tem_site_real, StatusFunil.NOVO

    if veredito.nada_foi_verificado:
        return None, StatusFunil.VERIFICAR_SITE

    return veredito.tem_site_real, StatusFunil.VERIFICAR_SITE


@transaction.atomic
def _persistir(
    varredura: Varredura,
    candidatos: list[ProspectCandidate],
    vereditos: list[SiteVerdict],
    site_confiavel: bool = True,
) -> tuple[int, int]:
    """Grava os candidatos SEM site. Devolve (sem_site, novos).

    `site_confiavel` vem da fonte (`FonteDeProspects.SITE_CONFIAVEL`) e
    decide se "sem site" qualifica sozinho ou só levanta suspeita.
    """
    # Lido do `origem_id` do próprio descarte, não do prospect: o veto tem de
    # valer mesmo que o prospect tenha sido apagado depois.
    banidos = set(
        Descarte.objects.filter(banido=True)
        .exclude(origem_id="")
        .values_list("origem_id", flat=True)
    )

    sem_site = 0
    novos = 0

    for candidato, veredito in zip(candidatos, vereditos, strict=True):
        if veredito.tem_site_real:
            continue

        sem_site += 1

        if candidato.origem_id in banidos:
            continue

        tem_site, status = _situacao_do_site(veredito, site_confiavel)
        telefone = analisar_telefone(candidato.telefone)

        existente = Prospect.objects.filter(
            origem=candidato.origem,
            origem_id=candidato.origem_id,
        ).first()

        if existente is None:
            origem_localizacao = _origem_da_localizacao(candidato)
            Prospect.objects.create(
                origem=candidato.origem,
                origem_id=candidato.origem_id,
                nome=candidato.nome,
                segmento=varredura.segmento,
                nicho=varredura.nicho,
                termo_busca=varredura.termo_busca,
                **_valores_de_localizacao(candidato),
                origem_localizacao=origem_localizacao,
                telefone=telefone.e164 or candidato.telefone,
                telefone_tipo=telefone.tipo,
                website_url=candidato.website_url,
                tem_site_real=tem_site,
                site_evidencia=veredito.evidencia,
                rating=candidato.rating,
                total_avaliacoes=candidato.total_avaliacoes,
                status_funil=status,
                ativo_no_funil=False,
                varredura=varredura,
            )
            novos += 1
            continue

        # Recaptura: atualiza o que é fato do mundo (telefone, site, nota) e
        # nunca toca no que foi decidido por você.
        existente.nome = candidato.nome
        _atualizar_localizacao(existente, candidato)
        existente.telefone = telefone.e164 or candidato.telefone
        existente.telefone_tipo = telefone.tipo
        existente.website_url = candidato.website_url
        existente.tem_site_real = tem_site
        existente.site_evidencia = veredito.evidencia
        existente.rating = candidato.rating
        existente.total_avaliacoes = candidato.total_avaliacoes

        if not existente.revisado_manualmente:
            existente.segmento = varredura.segmento

            # Só a fonte não confiável mexe em status na recaptura, e só em
            # quem ainda não foi revisado. O fluxo do Google segue sem tocar
            # em status_funil na recaptura, como sempre foi.
            if not site_confiavel:
                existente.status_funil = status
                existente.ativo_no_funil = False

        existente.save()

    return sem_site, novos


_CAMPOS_LOCALIZACAO = (
    "endereco",
    "bairro",
    "cidade",
    "estado",
    "pais",
    "cep",
    "latitude",
    "longitude",
)
_CAMPOS_LOCALIZACAO_SEM_COORDENADAS = (
    "endereco",
    "bairro",
    "cidade",
    "estado",
    "pais",
    "cep",
)


def _origem_da_localizacao(candidato: ProspectCandidate) -> str:
    if any(
        valor is not None for valor in _valores_de_localizacao(candidato).values()
    ):
        return OrigemLocalizacao.FONTE
    return OrigemLocalizacao.DESCONHECIDA


def _valores_de_localizacao(candidato: ProspectCandidate) -> dict[str, object | None]:
    valores = {
        campo: _valor_de_localizacao(getattr(candidato, campo))
        for campo in _CAMPOS_LOCALIZACAO_SEM_COORDENADAS
    }
    latitude, longitude = _par_de_coordenadas_valido(
        candidato.latitude,
        candidato.longitude,
    )
    valores["latitude"] = latitude
    valores["longitude"] = longitude
    return valores


def _valor_de_localizacao(valor: object) -> object | None:
    """Trata o legado ``""`` como ausência, sem tocar no parser Google."""
    if isinstance(valor, str) and not valor.strip():
        return None
    return valor


def _par_de_coordenadas_valido(
    latitude: object,
    longitude: object,
) -> tuple[float | None, float | None]:
    """Normaliza coordenadas somente quando a fonte declarou um par válido.

    A regra vive aqui, na fronteira comum das fontes: parser algum pode gravar
    latitude ou longitude isoladamente, nem deixar NaN/infinito atravessar
    para o banco.
    """
    if not _coordenada_valida(latitude, -90, 90):
        return None, None
    if not _coordenada_valida(longitude, -180, 180):
        return None, None
    return float(latitude), float(longitude)


def _coordenada_valida(valor: object, minimo: float, maximo: float) -> bool:
    return (
        isinstance(valor, (int, float))
        and not isinstance(valor, bool)
        and math.isfinite(valor)
        and minimo <= valor <= maximo
    )


def _atualizar_localizacao(existente: Prospect, candidato: ProspectCandidate) -> None:
    """Atualiza apenas fatos recebidos, preservando correção humana.

    A proveniência é do conjunto porque uma edição humana pode relacionar
    endereço, CEP e coordenadas. Escolhemos preservar o conjunto inteiro em
    vez de misturar fonte e correção manual sem evidência por campo.
    """
    if existente.origem_localizacao == OrigemLocalizacao.MANUAL:
        return

    valores = _valores_de_localizacao(candidato)
    recebeu_localizacao = False
    for campo in _CAMPOS_LOCALIZACAO:
        valor = valores[campo]
        if valor is not None:
            setattr(existente, campo, valor)
            recebeu_localizacao = True

    if (
        recebeu_localizacao
        and existente.origem_localizacao == OrigemLocalizacao.DESCONHECIDA
        and _localizacao_atual_veio_do_candidato(existente, valores)
    ):
        existente.origem_localizacao = OrigemLocalizacao.FONTE


def _localizacao_atual_veio_do_candidato(
    prospect: Prospect,
    valores_do_candidato: dict[str, object | None],
) -> bool:
    """Não transforma histórico ambíguo em fato declarado pela fonte.

    Como a proveniência é do conjunto, um prospect ``DESCONHECIDA`` só vira
    ``FONTE`` quando cada campo que restou preenchido foi recebido nesta mesma
    resposta. Latitude e longitude já chegam normalizadas como par indivisível.
    """
    return all(
        valores_do_candidato[campo] is not None
        for campo in _CAMPOS_LOCALIZACAO
        if _valor_de_localizacao(getattr(prospect, campo)) is not None
    )


def executar_varredura(
    *,
    nicho: Nicho,
    segmento: str,
    consulta: str,
    cidade: str,
    estado: str,
    quadrante: Quadrante | None = None,
    fonte: str = FONTE_PADRAO,
) -> Varredura:
    """Roda uma varredura completa e devolve o registro com os contadores.

    `nicho`, `segmento` e `consulta` já chegam resolvidos pelo caso de uso que
    iniciou a captação. `fonte` é o nome registrado em `leads.sources.FONTES`.
    O default mantém o Google Places como fonte primária.

    Raises:
        FranquiaEsgotadaError: se o teto mensal DESTA fonte já foi atingido.
    """
    if saldo_da_franquia(fonte) <= 0:
        raise FranquiaEsgotadaError(
            f"Franquia mensal esgotada em {fonte} ({consumo_do_mes(fonte)}/"
            f"{teto_da_franquia(fonte)} requisições). "
            "Captação bloqueada até o reset."
        )

    varredura = Varredura.objects.create(
        termo_busca=consulta,
        segmento=segmento,
        nicho=nicho,
        fonte=classe_da_fonte(fonte).ORIGEM,
        cidade=cidade,
        estado=estado,
        quadrante=quadrante,
        status=StatusVarredura.RODANDO,
    )

    try:
        celula = (
            CelulaGrade(
                rotulo=quadrante.rotulo,
                sul=quadrante.sul,
                oeste=quadrante.oeste,
                norte=quadrante.norte,
                leste=quadrante.leste,
            )
            if quadrante
            else None
        )

        coleta = asyncio.run(_coletar(consulta, celula, fonte, segmento))
        sem_site, novos = _persistir(
            varredura,
            coleta.candidatos,
            coleta.vereditos,
            site_confiavel=classe_da_fonte(fonte).SITE_CONFIAVEL,
        )

        varredura.total_requisicoes = coleta.requisicoes
        varredura.total_encontrados = len(coleta.candidatos)
        varredura.total_sem_site = sem_site
        varredura.total_novos = novos
        varredura.total_fechados = coleta.fechados
        varredura.status = StatusVarredura.CONCLUIDA
        varredura.concluido_em = timezone.now()
        varredura.save()
    except BuscaParcialError as exc:
        # Requisição já consumida conta na franquia mesmo com a busca em erro.
        _finalizar_varredura_ou_falhar(
            varredura,
            exc,
            total_requisicoes=exc.total_requisicoes,
        )
        logger.warning("Varredura %s terminou em erro parcial", varredura.id)
        raise VarreduraParcialError(varredura=varredura, causa=exc) from exc
    except Exception as exc:
        _finalizar_varredura_ou_falhar(varredura, exc)
        logger.exception("Varredura %s terminou em erro inesperado", varredura.id)
        raise

    return varredura


def _finalizar_varredura_com_erro(
    varredura: Varredura,
    exc: BaseException,
    *,
    total_requisicoes: int | None = None,
) -> None:
    """Tenta persistir o encerramento da Varredura como ERRO."""
    if total_requisicoes is not None:
        varredura.total_requisicoes = total_requisicoes
    varredura.status = StatusVarredura.ERRO
    varredura.erro = str(exc)
    varredura.concluido_em = timezone.now()
    varredura.save()


def _finalizar_varredura_ou_falhar(
    varredura: Varredura,
    causa_operacional: Exception,
    *,
    total_requisicoes: int | None = None,
) -> None:
    """Preserva separadamente a falha operacional e a de persistência."""
    try:
        _finalizar_varredura_com_erro(
            varredura,
            causa_operacional,
            total_requisicoes=total_requisicoes,
        )
    except Exception as causa_finalizacao:
        logger.exception(
            "Não foi possível persistir o status ERRO da Varredura %s",
            varredura.id,
        )
        raise FalhaFinalizacaoVarreduraError(
            varredura=varredura,
            causa_operacional=causa_operacional,
            causa_finalizacao=causa_finalizacao,
        ) from causa_finalizacao
