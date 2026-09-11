"""Filtro de qualificação: o estabelecimento já tem site próprio?

Este é o critério comercial da VITRE. Quem NÃO tem site é o alvo; quem tem,
sai do funil. O `websiteUri` da Places API não responde isso sozinho — ele
vem preenchido com Instagram, link-in-bio, página de agendamento, domínio
morto e página estacionada.

Contrato:

    async with SiteValidator() as v:
        veredito = await v.validar("https://instagram.com/salaox")
        # veredito.tem_site_real is False  → prospect VÁLIDO (é alvo)

Três camadas:

  L1 — estático. URL vazia, ou domínio na blacklist (social/agregador/
       agendamento/encurtador). Não gasta rede.
  L2 — HTTP. GET seguindo redirect: status, destino final, DNS/timeout.
       Um redirect que termina em wa.me continua não sendo site.
  L3 — conteúdo. Com 2xx na mão: página em branco, domínio estacionado,
       ou redirect por JavaScript para rede social.

O veredito carrega a EVIDÊNCIA, não só o booleano — é o que permite auditar
e recalibrar depois dos primeiros lotes reais, em vez de discutir no escuro.

Nenhuma chamada de rede acontece no import. Logs registram só a categoria do
resultado, nunca a URL completa nem o corpo da resposta.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yaml

logger = logging.getLogger(__name__)

_BLACKLIST_PATH = Path(__file__).with_name("blacklist.yml")

# Teto de caracteres lidos do corpo. Um site institucional real cabe muito
# abaixo disso; o teto evita baixar HTML gigante só para depois descartar.
_MAX_HTML_CHARS = 512 * 1024

# Alguns servidores recusam cliente sem User-Agent de browser.
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# Remove <script>/<style> INCLUINDO o conteúdo — a backreference \1 garante
# que casamos até a tag de fechamento correspondente.
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_JS_REDIRECT_RE = re.compile(
    r"""window\.location(?:\.href)?\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE,
)

# Abaixo disso, a página não tem conteúdo de verdade.
_MIN_TEXTO_VISIVEL = 100
# Termo de "parked" só conta se a página também for curta. 250 e não 500:
# página de "em construção" real quase sempre tem pouquíssimo texto, enquanto
# um site pequeno legítimo passa fácil dos 300 caracteres só com endereço e
# horário de funcionamento — e não pode morrer por escrever "em breve".
_MAX_TEXTO_PARKED = 250


# Evidência do único veredito em que NADA foi verificado: a fonte não deu
# URL. É constante porque a orquestração precisa distinguir "checamos e não
# há site" de "não tínhamos o que checar" — a diferença entre um prospect
# qualificado e um palpite.
EVIDENCIA_SEM_URL = "sem website"


@dataclass(frozen=True, slots=True)
class SiteVerdict:
    """Veredito sobre a URL de um estabelecimento.

    Attributes:
        tem_site_real: True se há site próprio ativo (→ descartar o prospect).
            False se não há (→ prospect é alvo da VITRE).
        evidencia: motivo legível, persistido em `Prospect.site_evidencia`.
    """

    tem_site_real: bool
    evidencia: str

    @property
    def nada_foi_verificado(self) -> bool:
        """True quando o False veio de ausência de URL, não de checagem."""
        return self.evidencia == EVIDENCIA_SEM_URL


def _dominio(url: str) -> str:
    """Host em minúsculas, sem `www.` e sem porta."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    host = urlparse(url).netloc.lower().split(":", 1)[0]
    return host[4:] if host.startswith("www.") else host


def _na_blacklist(url: str, dominios: frozenset[str]) -> bool:
    """True se o host casa por sufixo com algum domínio da lista.

    Sufixo, não substring: "instagram.com" casa "m.instagram.com" mas não
    casa "naoinstagram.com".
    """
    host = _dominio(url)
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in dominios)


class SiteValidator:
    """Decide se estabelecimentos têm site próprio ativo.

    A blacklist é lida do `blacklist.yml` no construtor. As requisições HTTP
    são limitadas por Semaphore — a camada L2 não custa dinheiro, mas exige
    disciplina para não sobrecarregar rede nem provocar bloqueio de IP.

    O client httpx pode ser injetado (os testes usam respx); quando omitido,
    o validator cria o seu e o fecha ao sair do context manager.
    """

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 5.0,
        max_concorrencia: int = 8,
        blacklist_path: Path | None = None,
    ) -> None:
        ignorados, parked = self._carregar_blacklist(blacklist_path or _BLACKLIST_PATH)
        self._ignorados = ignorados
        self._parked = parked

        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": _USER_AGENT},
        )
        self._sem = asyncio.Semaphore(max_concorrencia)

    @staticmethod
    def _carregar_blacklist(path: Path) -> tuple[frozenset[str], tuple[str, ...]]:
        dados = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        ignorados = frozenset(str(d).lower() for d in dados.get("ignored_domains", []))
        parked = tuple(str(t).lower() for t in dados.get("parked_terms", []))
        return ignorados, parked

    async def __aenter__(self) -> SiteValidator:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def validar(self, url: str | None) -> SiteVerdict:
        """Avalia `url`.

        Só devolve tem_site_real=True para site próprio, ativo e com conteúdo
        real. Ausente, rede social, DNS morto, erro HTTP ou domínio
        estacionado resultam em False — ou seja, prospect válido.
        """
        # L1a — sem URL. ATENÇÃO: aqui nada foi verificado — a fonte não deu
        # endereço para checar. Só é evidência de "não tem site" quando a
        # fonte popula `website` de forma confiável (Google sim, Foursquare
        # não). Quem consome distingue este veredito pela evidência, que é a
        # constante EVIDENCIA_SEM_URL.
        if not url or not url.strip():
            return SiteVerdict(tem_site_real=False, evidencia=EVIDENCIA_SEM_URL)

        url = url.strip()

        # L1b — domínio de terceiro (social, agregador, agendamento, encurtador).
        if _na_blacklist(url, self._ignorados):
            return SiteVerdict(tem_site_real=False, evidencia="só perfil/plataforma de terceiro")

        async with self._sem:
            return await self._validar_http(url)

    async def _validar_http(self, url: str) -> SiteVerdict:
        """L2 + L3. Assume que L1 já passou."""
        try:
            resposta = await self._client.get(url)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            # DNS inexistente, timeout, conexão recusada — domínio abandonado.
            logger.info("SiteValidator: falha de rede (%s) → sem site", type(exc).__name__)
            return SiteVerdict(tem_site_real=False, evidencia="dns/conexão")

        if not resposta.is_success:
            return SiteVerdict(tem_site_real=False, evidencia=f"http {resposta.status_code}")

        # Encurtador ou redirect que termina em rede social.
        if _na_blacklist(str(resposta.url), self._ignorados):
            return SiteVerdict(tem_site_real=False, evidencia="redirect p/ perfil de terceiro")

        return self._validar_conteudo(resposta.text[:_MAX_HTML_CHARS])

    def _validar_conteudo(self, html: str) -> SiteVerdict:
        """L3 — pelo HTML, distingue site real de casca.

        A ordem das checagens vai do sinal mais específico para o mais
        genérico, para a evidência gravada ser a mais informativa possível:
        um redirect por JS para o Instagram é um fato mais útil de auditar do
        que "conteúdo vazio", e as três levam ao mesmo veredito de qualquer
        forma.
        """
        html_lower = html.lower()

        sem_scripts = _SCRIPT_STYLE_RE.sub(" ", html_lower)
        texto_visivel = " ".join(_TAG_RE.sub(" ", sem_scripts).split())

        # 1. Redirect por JavaScript para perfil de terceiro. Buscado no HTML
        # cru — o <script> foi removido do texto visível, mas é justamente ali
        # que o redirect mora.
        for destino in _JS_REDIRECT_RE.findall(html_lower):
            if _na_blacklist(destino, self._ignorados):
                return SiteVerdict(
                    tem_site_real=False,
                    evidencia="js-redirect p/ perfil de terceiro",
                )

        # 2. Domínio estacionado / em construção: termo indicador numa página
        # curta. Busca no texto visível, não no HTML cru, para o termo em
        # <meta> ou atributo de um site real não gerar falso positivo.
        if len(texto_visivel) < _MAX_TEXTO_PARKED:
            for termo in self._parked:
                if termo in texto_visivel:
                    return SiteVerdict(tem_site_real=False, evidencia="parked/em construção")

        # 3. Casca sem conteúdo.
        if len(texto_visivel) < _MIN_TEXTO_VISIVEL:
            return SiteVerdict(tem_site_real=False, evidencia="conteúdo vazio")

        return SiteVerdict(tem_site_real=True, evidencia="site ativo")
