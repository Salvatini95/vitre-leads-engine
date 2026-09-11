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
- Prospect com descarte `banido` não volta ao funil.
- `termo_busca` guarda o termo da PRIMEIRA captura e não é sobrescrito.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from leads.filters.site_validator import SiteValidator, SiteVerdict
from leads.models import Descarte, Prospect, Quadrante, StatusFunil, StatusVarredura, Varredura
from leads.services.grade import CelulaGrade
from leads.sources import FONTE_PADRAO, classe_da_fonte, criar_fonte
from leads.sources.models import BuscaParcialError, ProspectCandidate

logger = logging.getLogger(__name__)

# A franquia mensal da Google reseta à meia-noite do dia 1 no fuso do
# Pacífico, não no nosso. Contar por mês local adiantaria o reset e deixaria
# a trava de custo furada nos primeiros dias do mês. O mesmo fuso vale para a
# Foursquare: não sabemos o reset dela, e este é o mais conservador dos dois.
_FUSO_FRANQUIA = ZoneInfo("America/Los_Angeles")


class FranquiaEsgotadaError(Exception):
    """Teto mensal de requisições atingido — captação bloqueada."""


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


def _montar_query(segmento_rotulo: str, cidade: str, estado: str) -> str:
    return f"{segmento_rotulo} em {cidade} {estado}"


async def _coletar(
    texto_query: str,
    celula: CelulaGrade | None,
    fonte: str = FONTE_PADRAO,
    segmento: str | None = None,
) -> tuple[list[ProspectCandidate], int, list[SiteVerdict]]:
    """Parte de rede: busca na fonte e valida o site de cada candidato.

    Devolve (candidatos, requisicoes_consumidas, vereditos) — os vereditos na
    mesma ordem dos candidatos. Em falha parcial da busca, o que já foi
    coletado é validado e devolvido mesmo assim, e o erro é propagado depois
    de o chamador gravar o custo.

    A validação de site é idêntica para qualquer fonte: o critério comercial
    ("não tem site") não muda porque o endereço veio de outra API.
    """
    async with criar_fonte(fonte) as cliente:
        resultado = await cliente.buscar(texto_query, celula, segmento)

    candidatos = [c for c in resultado.candidatos if c.ativo]

    async with SiteValidator() as validador:
        vereditos = await asyncio.gather(
            *(validador.validar(c.website_url) for c in candidatos)
        )

    return candidatos, resultado.total_requisicoes, list(vereditos)


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

        existente = Prospect.objects.filter(
            origem=candidato.origem,
            origem_id=candidato.origem_id,
        ).first()

        if existente is None:
            Prospect.objects.create(
                origem=candidato.origem,
                origem_id=candidato.origem_id,
                nome=candidato.nome,
                segmento=varredura.segmento,
                termo_busca=varredura.termo_busca,
                endereco=candidato.endereco,
                cidade=varredura.cidade,
                estado=varredura.estado,
                telefone=candidato.telefone,
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
        existente.endereco = candidato.endereco
        existente.telefone = candidato.telefone
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


def executar_varredura(
    *,
    segmento: str,
    segmento_rotulo: str,
    cidade: str,
    estado: str,
    quadrante: Quadrante | None = None,
    fonte: str = FONTE_PADRAO,
) -> Varredura:
    """Roda uma varredura completa e devolve o registro com os contadores.

    `fonte` é o nome registrado em `leads.sources.FONTES`. O default mantém o
    Google Places como fonte primária.

    Raises:
        FranquiaEsgotadaError: se o teto mensal DESTA fonte já foi atingido.
    """
    if saldo_da_franquia(fonte) <= 0:
        raise FranquiaEsgotadaError(
            f"Franquia mensal esgotada em {fonte} ({consumo_do_mes(fonte)}/"
            f"{teto_da_franquia(fonte)} requisições). "
            "Captação bloqueada até o reset."
        )

    texto_query = _montar_query(segmento_rotulo, cidade, estado)

    varredura = Varredura.objects.create(
        termo_busca=texto_query,
        segmento=segmento,
        fonte=classe_da_fonte(fonte).ORIGEM,
        cidade=cidade,
        estado=estado,
        quadrante=quadrante,
        status=StatusVarredura.RODANDO,
    )

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

    try:
        candidatos, requisicoes, vereditos = asyncio.run(
            _coletar(texto_query, celula, fonte, segmento)
        )
    except BuscaParcialError as exc:
        # Requisição já consumida conta na franquia mesmo com a busca em erro.
        varredura.total_requisicoes = exc.total_requisicoes
        varredura.status = StatusVarredura.ERRO
        varredura.erro = str(exc)
        varredura.concluido_em = timezone.now()
        varredura.save()
        logger.warning("Varredura %s terminou em erro parcial", varredura.id)
        raise

    sem_site, novos = _persistir(
        varredura,
        candidatos,
        vereditos,
        site_confiavel=classe_da_fonte(fonte).SITE_CONFIAVEL,
    )

    varredura.total_requisicoes = requisicoes
    varredura.total_encontrados = len(candidatos)
    varredura.total_sem_site = sem_site
    varredura.total_novos = novos
    varredura.status = StatusVarredura.CONCLUIDA
    varredura.concluido_em = timezone.now()
    varredura.save()

    return varredura
