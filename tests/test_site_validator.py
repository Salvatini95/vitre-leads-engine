"""Testes do filtro de qualificação.

Cada caso aqui é um jeito real de o `websiteUri` do Google mentir. A regra a
memorizar: tem_site_real=False significa PROSPECT VÁLIDO (é alvo da VITRE).
"""

from __future__ import annotations

import httpx
import pytest
import respx

from leads.filters.site_validator import SiteValidator

SITE_REAL_HTML = """
<html><head><title>Salão Bella</title></head><body>
<h1>Salão Bella Maringá</h1>
<p>Somos um salão de beleza no centro de Maringá com 15 anos de história.
Trabalhamos com corte, coloração, escova, tratamentos capilares, manicure e
pedicure. Agende pelo telefone ou passe na loja. Rua Santos Dumont, 1234.
Horário: segunda a sábado das 9h às 19h. Estacionamento próprio.</p>
</body></html>
"""

PARKED_HTML = """
<html><body><h1>Em construção</h1>
<p>Este site está em construção. Volte em breve.</p></body></html>
"""

VAZIO_HTML = "<html><body><div></div></body></html>"

JS_REDIRECT_HTML = """
<html><head><script>window.location.href = "https://instagram.com/salaox";</script></head>
<body><p>Redirecionando para nosso perfil. Aguarde um momento por favor,
voce sera levado ate a nossa pagina oficial em instantes. Obrigado pela
visita e ate ja no nosso perfil oficial da rede social.</p></body></html>
"""


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["", "   ", None])
async def test_sem_url_e_alvo(url):
    async with SiteValidator() as v:
        veredito = await v.validar(url)
    assert veredito.tem_site_real is False
    assert veredito.evidencia == "sem website"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://instagram.com/salaobella",
        "https://www.instagram.com/salaobella",
        "https://m.instagram.com/salaobella",
        "https://facebook.com/salaobella",
        "https://linktr.ee/salaobella",
        "https://wa.me/5544999999999",
        "https://trinks.com.br/salao/bella",
        "https://negocio.site/salao-bella",
        "https://bit.ly/salaobella",
    ],
)
async def test_perfil_de_terceiro_e_alvo(url):
    """Instagram, link-in-bio, agendamento e encurtador não são site próprio."""
    async with SiteValidator() as v:
        veredito = await v.validar(url)
    assert veredito.tem_site_real is False
    assert veredito.evidencia == "só perfil/plataforma de terceiro"


@pytest.mark.asyncio
async def test_dominio_parecido_nao_casa_por_substring():
    """"naoinstagram.com.br" não pode cair na blacklist de "instagram.com"."""
    with respx.mock:
        respx.get("https://naoinstagram.com.br").mock(
            return_value=httpx.Response(200, html=SITE_REAL_HTML)
        )
        async with SiteValidator() as v:
            veredito = await v.validar("https://naoinstagram.com.br")
    assert veredito.tem_site_real is True


@pytest.mark.asyncio
async def test_site_proprio_ativo_nao_e_alvo():
    with respx.mock:
        respx.get("https://salaobella.com.br").mock(
            return_value=httpx.Response(200, html=SITE_REAL_HTML)
        )
        async with SiteValidator() as v:
            veredito = await v.validar("https://salaobella.com.br")
    assert veredito.tem_site_real is True
    assert veredito.evidencia == "site ativo"


@pytest.mark.asyncio
async def test_http_404_e_alvo():
    with respx.mock:
        respx.get("https://salaomorto.com.br").mock(return_value=httpx.Response(404))
        async with SiteValidator() as v:
            veredito = await v.validar("https://salaomorto.com.br")
    assert veredito.tem_site_real is False
    assert veredito.evidencia == "http 404"


@pytest.mark.asyncio
async def test_dns_morto_e_alvo():
    with respx.mock:
        respx.get("https://dominioinexistente.com.br").mock(
            side_effect=httpx.ConnectError("nome não resolve")
        )
        async with SiteValidator() as v:
            veredito = await v.validar("https://dominioinexistente.com.br")
    assert veredito.tem_site_real is False
    assert veredito.evidencia == "dns/conexão"


@pytest.mark.asyncio
async def test_timeout_e_alvo():
    with respx.mock:
        respx.get("https://lento.com.br").mock(side_effect=httpx.ReadTimeout("estourou"))
        async with SiteValidator() as v:
            veredito = await v.validar("https://lento.com.br")
    assert veredito.tem_site_real is False
    assert veredito.evidencia == "dns/conexão"


@pytest.mark.asyncio
async def test_redirect_final_para_whatsapp_e_alvo():
    """Domínio próprio que só redireciona para o WhatsApp não é site."""
    with respx.mock:
        respx.get("https://salaoredirect.com.br").mock(
            return_value=httpx.Response(
                302, headers={"Location": "https://wa.me/5544999999999"}
            )
        )
        respx.get("https://wa.me/5544999999999").mock(
            return_value=httpx.Response(200, html="<html><body>WhatsApp</body></html>")
        )
        async with SiteValidator() as v:
            veredito = await v.validar("https://salaoredirect.com.br")
    assert veredito.tem_site_real is False
    assert veredito.evidencia == "redirect p/ perfil de terceiro"


@pytest.mark.asyncio
async def test_pagina_em_construcao_e_alvo():
    with respx.mock:
        respx.get("https://salaonovo.com.br").mock(
            return_value=httpx.Response(200, html=PARKED_HTML)
        )
        async with SiteValidator() as v:
            veredito = await v.validar("https://salaonovo.com.br")
    assert veredito.tem_site_real is False
    assert veredito.evidencia == "parked/em construção"


@pytest.mark.asyncio
async def test_pagina_vazia_e_alvo():
    with respx.mock:
        respx.get("https://casca.com.br").mock(
            return_value=httpx.Response(200, html=VAZIO_HTML)
        )
        async with SiteValidator() as v:
            veredito = await v.validar("https://casca.com.br")
    assert veredito.tem_site_real is False
    assert veredito.evidencia == "conteúdo vazio"


@pytest.mark.asyncio
async def test_js_redirect_para_instagram_e_alvo():
    with respx.mock:
        respx.get("https://salaojs.com.br").mock(
            return_value=httpx.Response(200, html=JS_REDIRECT_HTML)
        )
        async with SiteValidator() as v:
            veredito = await v.validar("https://salaojs.com.br")
    assert veredito.tem_site_real is False
    assert veredito.evidencia == "js-redirect p/ perfil de terceiro"


@pytest.mark.asyncio
async def test_termo_de_parked_em_site_longo_nao_e_falso_positivo():
    """Site real que escreve "em breve" numa seção continua sendo site."""
    html = SITE_REAL_HTML.replace(
        "</p>", " Em breve inauguramos a segunda unidade na zona sul.</p>"
    )
    with respx.mock:
        respx.get("https://salaogrande.com.br").mock(
            return_value=httpx.Response(200, html=html)
        )
        async with SiteValidator() as v:
            veredito = await v.validar("https://salaogrande.com.br")
    assert veredito.tem_site_real is True
