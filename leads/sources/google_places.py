"""Cliente da Google Places API (New) — Text Search.

Endpoint: POST https://places.googleapis.com/v1/places:searchText
Doc: https://developers.google.com/maps/documentation/places/web-service/text-search

Três detalhes desta API que quebram a captação em silêncio se ignorados, e que
por isso estão tratados explicitamente aqui:

1. O `X-Goog-FieldMask` filtra a resposta INTEIRA, não só cada `places.*`.
   `nextPageToken` é campo de TOPO — se não estiver no mask, a API não o
   devolve, a paginação nunca passa da 1ª página, e a busca "termina" com 20
   resultados parecendo que a cidade acabou. Falha silenciosa, sem erro.

2. O `nextPageToken` recém-emitido leva 1–2 s para valer. A primeira tentativa
   de usá-lo pode voltar 400 INVALID_ARGUMENT transitório — não é bug de
   código nem token inválido, é propagação.

3. Cada campo no mask muda o SKU de cobrança. O mask abaixo é o mínimo para o
   domínio da VITRE; não acrescentar campo sem saber o que muda na fatura.

Autenticação por header `X-Goog-Api-Key`. A chave nunca é logada — o código
registra apenas presença.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from leads.services.grade import CelulaGrade
from leads.sources.base import FonteDeProspects
from leads.sources.models import BuscaParcialError, ProspectCandidate, ResultadoBusca

logger = logging.getLogger(__name__)

_ENDPOINT = "https://places.googleapis.com/v1/places:searchText"

# 20 resultados por página x 3 páginas = até 60 por busca, que é o teto da
# própria API. Subir isto não traz mais resultado — o que traz é subdividir a
# cidade em mais quadrantes (ver leads.services.grade).
_MAX_PAGINAS = 3

# Teto para o valor de Retry-After: header malformado ou hostil não pode
# travar a captação por minutos.
_RETRY_AFTER_TETO = 8.0

_TOKEN_RETRY_TENTATIVAS = 3
_TOKEN_RETRY_ESPERA = 1.5

# `businessStatus` fora de OPERATIONAL não vira prospect. Os dois entram, e
# não só o permanente, porque o equivalente da Foursquare (`date_closed`) não
# separa permanente de temporário — manter só o permanente aqui deixaria as
# duas fontes com critérios diferentes de "fechado", que é exatamente o tipo
# de divergência que faz a fila de uma parecer mais suja que a da outra sem
# ninguém saber por quê. Estabelecimento de portas fechadas não compra site.
_STATUS_FECHADO = frozenset({"CLOSED_PERMANENTLY", "CLOSED_TEMPORARILY"})

# `nextPageToken` PRECISA vir aqui — ver observação 1 no topo do módulo.
_FIELD_MASK = ",".join(
    [
        "nextPageToken",
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.nationalPhoneNumber",
        "places.websiteUri",
        "places.rating",
        "places.userRatingCount",
        "places.businessStatus",
    ]
)


def _e_retentavel(exc: BaseException) -> bool:
    """5xx, 429 e falha de rede valem retry. 4xx (exceto 429) não."""
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status == 429 or 500 <= status < 600
    return False


def _espera_com_retry_after(state: RetryCallState) -> float:
    """Honra `Retry-After` num 429; senão, backoff exponencial."""
    padrao = wait_exponential(multiplier=1, max=_RETRY_AFTER_TETO)(state)

    exc = state.outcome.exception() if state.outcome else None
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
        cabecalho = exc.response.headers.get("Retry-After", "")
        try:
            segundos = float(cabecalho)
        except ValueError:
            return padrao
        if segundos <= 0:
            return padrao
        return min(segundos, _RETRY_AFTER_TETO)

    return padrao


class GooglePlacesSource(FonteDeProspects):
    """Busca estabelecimentos por texto, restrita a um retângulo geográfico.

        async with GooglePlacesSource(api_key) as fonte:
            resultado = await fonte.buscar("salão de beleza em Maringá PR", celula)

    O client httpx pode ser injetado nos testes.
    """

    NOME = "google_places"
    ORIGEM = "GOOGLE_PLACES"
    MAX_REQUISICOES_POR_BUSCA = _MAX_PAGINAS
    SETTING_FRANQUIA = "PLACES_FRANQUIA_MENSAL"
    # `websiteUri` do Google é populado de forma confiável: vazio aqui é
    # evidência de que o negócio não tem site. É a premissa que sustenta o
    # filtro da VITRE desde a Fase 1.
    SITE_CONFIAVEL = True

    def __init__(
        self,
        api_key: str,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 15.0,
    ) -> None:
        if not api_key:
            raise ValueError(
                "GOOGLE_PLACES_API_KEY ausente — preencha o .env antes de captar"
            )
        self._api_key = api_key
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(timeout=timeout)
        logger.info("GooglePlacesSource inicializado (api_key presente: True)")

    async def __aenter__(self) -> GooglePlacesSource:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def buscar(
        self,
        texto_query: str,
        celula: CelulaGrade | None = None,
        segmento: str | None = None,
        nicho_codigo: str | None = None,
    ) -> ResultadoBusca:
        """Busca paginada. Sem `celula`, busca sem recorte geográfico.

        `segmento` e `nicho_codigo` são ignorados aqui de propósito: o Text
        Search do Google entende a frase em português direto. Esses parâmetros
        existem no contrato comum porque outras fontes, como a Foursquare,
        precisam traduzi-los para categorias próprias.

        Raises:
            BuscaParcialError: falha de rede/HTTP, carregando o que já foi
                consumido da franquia até ali.
        """
        candidatos: list[ProspectCandidate] = []
        requisicoes = 0
        token: str | None = None

        for pagina in range(_MAX_PAGINAS):
            try:
                dados = await self._requisitar(texto_query, celula, token)
            except Exception as exc:
                raise BuscaParcialError(
                    total_requisicoes=requisicoes,
                    candidatos=candidatos,
                ) from exc

            requisicoes += 1
            candidatos.extend(self._parsear(dados.get("places", [])))

            token = dados.get("nextPageToken")
            if not token:
                break

            # Token recém-emitido precisa de um instante para propagar.
            if pagina < _MAX_PAGINAS - 1:
                await asyncio.sleep(_TOKEN_RETRY_ESPERA)

        return ResultadoBusca(candidatos=candidatos, total_requisicoes=requisicoes)

    @retry(
        retry=retry_if_exception(_e_retentavel),
        wait=_espera_com_retry_after,
        stop=stop_after_attempt(4),
        reraise=True,
    )
    async def _requisitar(
        self,
        texto_query: str,
        celula: CelulaGrade | None,
        token: str | None,
    ) -> dict[str, Any]:
        corpo: dict[str, Any] = {
            "textQuery": texto_query,
            "languageCode": "pt-BR",
            "regionCode": "BR",
            "pageSize": 20,
        }

        if celula is not None:
            corpo["locationRestriction"] = {
                "rectangle": {
                    "low": {"latitude": celula.sul, "longitude": celula.oeste},
                    "high": {"latitude": celula.norte, "longitude": celula.leste},
                }
            }

        if token:
            corpo["pageToken"] = token

        resposta = await self._client.post(
            _ENDPOINT,
            json=corpo,
            headers={
                "X-Goog-Api-Key": self._api_key,
                "X-Goog-FieldMask": _FIELD_MASK,
                "Content-Type": "application/json",
            },
        )
        resposta.raise_for_status()
        return resposta.json()

    @staticmethod
    def _parsear(places: list[dict[str, Any]]) -> list[ProspectCandidate]:
        candidatos: list[ProspectCandidate] = []
        for lugar in places:
            place_id = lugar.get("id")
            if not place_id:
                continue

            nome = (lugar.get("displayName") or {}).get("text", "")
            if not nome:
                continue

            # Ausente no field mask antigo ou em lugar sem o dado: o Google
            # omite o campo em vez de mandar OPERATIONAL. Ausência é "não
            # afirmou que fechou", não "confirmou que está aberto".
            situacao = lugar.get("businessStatus") or ""
            fechado = situacao in _STATUS_FECHADO

            candidatos.append(
                ProspectCandidate(
                    origem=GooglePlacesSource.ORIGEM,
                    origem_id=place_id,
                    nome=nome,
                    endereco=lugar.get("formattedAddress", "") or "",
                    telefone=lugar.get("nationalPhoneNumber", "") or "",
                    website_url=lugar.get("websiteUri", "") or "",
                    rating=lugar.get("rating"),
                    total_avaliacoes=lugar.get("userRatingCount"),
                    ativo=not fechado,
                    fechado_evidencia=(f"businessStatus={situacao}" if fechado else ""),
                )
            )
        return candidatos
