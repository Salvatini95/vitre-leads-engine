"""Contrato comum das fontes de captação.

Existia um contrato implícito — `GooglePlacesSource` já devolvia
`ResultadoBusca` e já era um async context manager — mas nada o declarava.
Com uma segunda fonte entrando (Foursquare), o contrato vira explícito aqui
para que a orquestração (`leads.services.captacao`) fale com QUALQUER fonte
sem saber qual é.

O que uma fonte precisa declarar, além de `buscar`:

- `NOME` — chave do `--fonte` na linha de comando.
- `ORIGEM` — valor gravado em `Prospect.origem` (tem de existir em
  `leads.models.Origem`, senão o dedup por (origem, origem_id) fura).
- `MAX_REQUISICOES_POR_BUSCA` — pior caso de páginas numa busca. É o que a
  estimativa de custo do comando multiplica pelo número de quadrantes.
- `SETTING_FRANQUIA` — nome do setting com o teto mensal DESTA fonte. Cada
  API tem franquia própria (Google: 1.000; Foursquare: 10.000); somar as duas
  no mesmo contador travaria a captação cedo demais numa e tarde demais na
  outra.
- `SITE_CONFIAVEL` — se o campo `website` da fonte é populado o bastante para
  que "veio vazio" signifique "não tem site". É o coração do critério
  comercial da VITRE, e varia por fonte: o Google popula, a Foursquare não
  (93% vazios em Maringá). Fonte com `False` não qualifica ninguém sozinha —
  seus prospects nascem pendentes de verificação manual.

Escala de `rating`: o contrato é 0–5, a escala do Google. Fonte que pontue em
outra escala normaliza ANTES de montar o `ProspectCandidate` — o Admin ordena
a curadoria por nota, e misturar escalas desordena a fila em silêncio.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from leads.services.grade import CelulaGrade
from leads.sources.models import ResultadoBusca


class FonteDeProspects(ABC):
    """Uma fonte de estabelecimentos buscável por texto e recorte geográfico.

        async with AlgumaFonte(api_key) as fonte:
            resultado = await fonte.buscar("salão de beleza em Maringá PR", celula)
    """

    NOME: ClassVar[str]
    ORIGEM: ClassVar[str]
    MAX_REQUISICOES_POR_BUSCA: ClassVar[int]
    SETTING_FRANQUIA: ClassVar[str]
    SITE_CONFIAVEL: ClassVar[bool]

    async def __aenter__(self) -> FonteDeProspects:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    @abstractmethod
    async def buscar(
        self,
        texto_query: str,
        celula: CelulaGrade | None = None,
        segmento: str | None = None,
    ) -> ResultadoBusca:
        """Busca paginada. Sem `celula`, busca sem recorte geográfico.

        `texto_query` é a frase montada pela orquestração ("salão de beleza em
        Maringá PR"). `segmento` é o código bruto (`Segmento.SALAO`) da mesma
        busca, e existe porque nem toda API entende busca por frase: o Google
        entende, e ignora o `segmento`; a Foursquare não entende, e precisa do
        código para filtrar por categoria. Sem esse parâmetro, a Foursquare
        devolve hotel e supermercado para uma busca por salão de beleza —
        falha silenciosa, o resultado parece cobertura ruim e é query errada.

        Raises:
            BuscaParcialError: falha de rede/HTTP, carregando o que já foi
                consumido da franquia até ali. Requisição gasta conta mesmo
                quando a busca morre no meio.
        """
