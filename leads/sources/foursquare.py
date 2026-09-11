"""Cliente da Foursquare Places API — Place Search.

Endpoint: GET {base}/places/search
Doc: https://docs.foursquare.com/fsq-developers-places/reference/place-search

Fonte de CONTINGÊNCIA, não substituta do Google Places: entrou porque o
billing do Google Cloud está bloqueado (ver `org-ia/05_estado.md`). A
cobertura da Foursquare para pequeno negócio de bairro no Brasil é
historicamente mais fraca que a do Google — o número de resultados por
quadrante é o que decide se vale manter.

Seis diferenças em relação ao cliente do Google que estão tratadas aqui e
que, ignoradas, corrompem dado em silêncio:

0. O `query` da Foursquare NÃO é um text search em português como o do
   Google. Medido em Maringá, `query="Salão de beleza em Maringá PR"` no
   quadrante central devolve hotel, universidade e supermercado — casa por
   relevância difusa e ignora o segmento. Pior: `query="salão de beleza"`
   (com acento) devolve ZERO. Por isso a busca aqui é por CATEGORIA
   (`fsq_category_ids`), com o texto usado só como último recurso, quando o
   segmento não tem categoria mapeada. Foi o que separou 20 salões reais de
   41 resultados de lixo no mesmo quadrante.

1. `rating` da Foursquare é 0–10; o do Google é 0–5. O contrato de
   `ProspectCandidate` é 0–5 (ver `leads.sources.base`), então a nota é
   dividida por 2 na entrada. Sem isso, o Admin — que ordena a curadoria por
   nota — jogaria todo prospect da Foursquare para o topo da fila.

2. Paginação é por cursor no header `Link`, não por token no corpo. Sem o
   header não há próxima página: a busca acaba ali, legitimamente.

3. Recorte geográfico é `ne`/`sw` (dois cantos, formato "lat,lng") em query
   string, e é MUTUAMENTE EXCLUSIVO com `ll`/`radius`. Mandar os dois é 400.

4. `fields` não é só filtro de resposta: campo não pedido não vem. Diferente
   do Google, aqui não muda o preço — muda o que existe no JSON.

5. Autenticação mudou entre gerações da API. A v3 legada
   (`api.foursquare.com/v3`) usa `Authorization: <chave>` crua; a geração
   atual (`places-api.foursquare.com`) usa `Authorization: Bearer <chave>`
   mais o header de versão, e renomeou `fsq_id` para `fsq_place_id`. O
   cliente deriva o estilo do host configurado em `FOURSQUARE_API_BASE` e
   aceita os dois nomes de id, porque qual dos dois a chave do operador fala
   só se descobre na primeira chamada real.

6. `rating` e `stats` são campos PAGOS — consomem crédito de API, não a
   franquia de chamadas. Pedi-los sem crédito devolve 429 e derruba a busca
   INTEIRA, não só a nota: não é rate limit de verdade, é cobrança. Ficam
   desligados por padrão (`campos_pro=False`), o que deixa `rating` e
   `total_avaliacoes` vazios nos prospects vindos daqui — e portanto tira
   deles a ordenação da fila de curadoria do Admin.

A chave nunca é logada — o código registra apenas presença.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

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

# Geração atual da API. Para uma chave v3 legada, apontar
# FOURSQUARE_API_BASE=https://api.foursquare.com/v3 no .env.
_BASE_PADRAO = "https://places-api.foursquare.com"
_CAMINHO = "/places/search"

# Versão do contrato da geração atual. Ignorada pela v3 legada.
_API_VERSION = "2025-06-17"

# 50 é o teto por página da própria API (o Google usa 20).
_TAMANHO_PAGINA = 50

# 3 páginas x 50 = até 150 por quadrante. O teto real da Foursquare por busca
# é mais generoso que o do Google, mas manter 3 páginas mantém a mesma ordem
# de grandeza de custo por quadrante nas duas fontes.
_MAX_PAGINAS = 3

_RETRY_AFTER_TETO = 8.0

# Campo não pedido não vem na resposta — ver observação 4 no topo do módulo.
#
# Este conjunto é o que a conta gratuita responde. Verificado contra a API:
# `closed_bucket` e `closed_status` (nomes da v3) devolvem 400 nesta geração;
# `date_closed` é o campo de fechamento que existe.
_FIELDS_BASE = (
    "fsq_place_id",
    "name",
    "location",
    "tel",
    "website",
    "categories",
    "date_closed",
)

# Campos PAGOS: consomem crédito de API, não a franquia de chamadas. A conta
# do operador está sem crédito hoje — pedir estes campos devolve 429
# ("no API credits remaining") e derruba a busca inteira, não só a nota.
# Por isso ficam desligados por padrão: sem eles a captação roda de graça,
# com `rating` e `total_avaliacoes` vazios.
_FIELDS_PRO = ("rating", "stats")

# Escala da Foursquare (0–10) para a do contrato (0–5).
_DIVISOR_RATING = 2.0

# Ids colhidos da própria API (o endpoint de taxonomia responde 404 nesta
# geração; estes vieram das respostas de busca em Maringá).
_HAIR_SALON = "4bf58dd8d48988d110951735"
_BARBERSHOP = "63be6904847c3692a84b9b49"
_BELEZA_GENERICA = "54541900498ea6ccd0202697"  # "Health and Beauty Service"
_NAIL_SALON = "4f04aa0c2fb6e1c99f3db0b8"
_SPA = "4bf58dd8d48988d1ed941735"
_MASSAGE_CLINIC = "52f2ab2ebcbc57f1066b8b3c"
_HAIR_REMOVAL = "63be6904847c3692a84b9b4a"

# Segmento da VITRE → categorias da Foursquare. Chaveado pelo código bruto de
# `leads.models.Segmento`, por string, para o cliente não depender do ORM.
#
# LASH e SOBRANCELHA são aproximações: a taxonomia da Foursquare não tem
# categoria para lash designer nem para design de sobrancelha, então caem na
# genérica de beleza e vão precisar de curadoria mais pesada.
# OUTRO fica de fora de propósito — sem segmento definido, não há categoria a
# escolher, e a busca cai no texto.
_CATEGORIAS_POR_SEGMENTO: dict[str, tuple[str, ...]] = {
    "SALAO": (_HAIR_SALON, _BARBERSHOP, _BELEZA_GENERICA),
    "ESTETICA": (_SPA, _MASSAGE_CLINIC, _BELEZA_GENERICA),
    "NAIL": (_NAIL_SALON,),
    "LASH": (_BELEZA_GENERICA,),
    "SOBRANCELHA": (_HAIR_REMOVAL, _BELEZA_GENERICA),
}

_LINK_NEXT = re.compile(r'<([^>]+)>\s*;\s*rel="next"')


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


def _cursor_da_resposta(resposta: httpx.Response) -> str | None:
    """Extrai o cursor da próxima página do header `Link`.

    Ausência de `Link` significa fim da paginação — não é erro.
    """
    link = resposta.headers.get("Link", "")
    if not link:
        return None

    achado = _LINK_NEXT.search(link)
    if not achado:
        return None

    cursores = parse_qs(urlparse(achado.group(1)).query).get("cursor")
    return cursores[0] if cursores else None


class FoursquareSource(FonteDeProspects):
    """Busca estabelecimentos por texto, restrita a um retângulo geográfico.

        async with FoursquareSource(api_key) as fonte:
            resultado = await fonte.buscar("salão de beleza em Maringá PR", celula)

    O client httpx pode ser injetado nos testes.
    """

    NOME = "foursquare"
    ORIGEM = "FOURSQUARE"
    MAX_REQUISICOES_POR_BUSCA = _MAX_PAGINAS
    SETTING_FRANQUIA = "FOURSQUARE_FRANQUIA_MENSAL"
    # Medido em Maringá: só 27 de 400 estabelecimentos (6,8%) vieram com
    # qualquer `website`. Campo vazio aqui é ausência de DADO, não ausência
    # de site — então esta fonte não qualifica ninguém sozinha.
    SITE_CONFIAVEL = False

    def __init__(
        self,
        api_key: str,
        *,
        api_base: str = _BASE_PADRAO,
        campos_pro: bool = False,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 15.0,
    ) -> None:
        if not api_key:
            raise ValueError(
                "FOURSQUARE_API_KEY ausente — preencha o .env antes de captar"
            )
        self._api_key = api_key
        self._campos = _FIELDS_BASE + (_FIELDS_PRO if campos_pro else ())
        self._api_base = api_base.rstrip("/")
        self._endpoint = f"{self._api_base}{_CAMINHO}"
        self._legado_v3 = self._api_base.endswith("/v3")
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(timeout=timeout)
        logger.info(
            "FoursquareSource inicializado (api_key presente: True, base: %s)",
            self._api_base,
        )

    async def __aenter__(self) -> FoursquareSource:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def buscar(
        self,
        texto_query: str,
        celula: CelulaGrade | None = None,
        segmento: str | None = None,
    ) -> ResultadoBusca:
        """Busca paginada. Sem `celula`, busca sem recorte geográfico.

        Com `segmento` mapeado, busca por categoria e IGNORA `texto_query` —
        ver observação 0 no topo do módulo. Sem mapeamento, cai no texto.

        Raises:
            BuscaParcialError: falha de rede/HTTP, carregando o que já foi
                consumido da franquia até ali.
        """
        candidatos: list[ProspectCandidate] = []
        requisicoes = 0
        cursor: str | None = None
        categorias = _CATEGORIAS_POR_SEGMENTO.get(segmento or "")

        if categorias is None:
            logger.warning(
                "Segmento %r sem categoria Foursquare mapeada — caindo na busca "
                "por texto, que nesta API traz muito ruído.",
                segmento,
            )

        for _ in range(_MAX_PAGINAS):
            try:
                dados, cursor = await self._requisitar(
                    texto_query, celula, cursor, categorias
                )
            except Exception as exc:
                raise BuscaParcialError(
                    total_requisicoes=requisicoes,
                    candidatos=candidatos,
                ) from exc

            requisicoes += 1
            candidatos.extend(self._parsear(dados.get("results", [])))

            if not cursor:
                break

        return ResultadoBusca(candidatos=candidatos, total_requisicoes=requisicoes)

    def _headers(self) -> dict[str, str]:
        """Estilo de auth conforme a geração da API — ver observação 5."""
        if self._legado_v3:
            return {"Authorization": self._api_key, "Accept": "application/json"}
        return {
            "Authorization": f"Bearer {self._api_key}",
            "X-Places-Api-Version": _API_VERSION,
            "Accept": "application/json",
        }

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
        cursor: str | None,
        categorias: tuple[str, ...] | None,
    ) -> tuple[dict[str, Any], str | None]:
        params: dict[str, Any] = {
            "limit": _TAMANHO_PAGINA,
            "fields": ",".join(self._campos),
        }

        # Categoria e texto juntos estreitam demais: a categoria já é o
        # filtro de segmento, e o texto ainda casaria contra o NOME.
        if categorias:
            params["fsq_category_ids"] = ",".join(categorias)
        else:
            params["query"] = texto_query

        if celula is not None:
            # ne = canto nordeste, sw = canto sudoeste. Só um par de cantos,
            # nunca junto com ll/radius — ver observação 3.
            params["ne"] = f"{celula.norte},{celula.leste}"
            params["sw"] = f"{celula.sul},{celula.oeste}"

        if cursor:
            params["cursor"] = cursor

        resposta = await self._client.get(
            self._endpoint,
            params=params,
            headers=self._headers(),
        )
        resposta.raise_for_status()
        return resposta.json(), _cursor_da_resposta(resposta)

    @staticmethod
    def _parsear(resultados: list[dict[str, Any]]) -> list[ProspectCandidate]:
        candidatos: list[ProspectCandidate] = []
        for lugar in resultados:
            # `fsq_place_id` na geração atual, `fsq_id` na v3 legada.
            place_id = lugar.get("fsq_place_id") or lugar.get("fsq_id")
            if not place_id:
                continue

            nome = lugar.get("name") or ""
            if not nome:
                continue

            localizacao = lugar.get("location") or {}
            categorias = lugar.get("categories") or []
            estatisticas = lugar.get("stats") or {}

            nota = lugar.get("rating")

            candidatos.append(
                ProspectCandidate(
                    origem=FoursquareSource.ORIGEM,
                    origem_id=place_id,
                    nome=nome,
                    endereco=localizacao.get("formatted_address", "") or "",
                    telefone=lugar.get("tel", "") or "",
                    website_url=lugar.get("website", "") or "",
                    # 0–10 na fonte, 0–5 no contrato — ver observação 1.
                    rating=(nota / _DIVISOR_RATING) if nota is not None else None,
                    total_avaliacoes=estatisticas.get("total_ratings"),
                    categoria=(categorias[0].get("name", "") if categorias else ""),
                    # `date_closed` preenchido é o equivalente do
                    # CLOSED_PERMANENTLY do Google: não há para quem vender.
                    ativo=not lugar.get("date_closed"),
                )
            )
        return candidatos
