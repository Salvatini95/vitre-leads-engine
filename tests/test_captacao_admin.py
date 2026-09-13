"""Fluxo Admin da captação por Cidade, sem chamadas externas."""

from __future__ import annotations

import inspect
import json
from html.parser import HTMLParser
from types import SimpleNamespace

import pytest
from django.contrib import admin as django_admin
from django.contrib.auth import get_user_model
from django.contrib.messages import ERROR, SUCCESS, WARNING, get_messages
from django.http import QueryDict
from django.test import Client
from django.urls import reverse

from leads import admin as leads_admin
from leads.admin import VarreduraAdmin
from leads.models import Nicho, Quadrante, Segmento, Varredura
from leads.services.captacao_cidade import (
    CaptacaoCidadeError,
    CaptacaoCidadeFalhaFinalizacaoError,
    CaptacaoCidadeParcialError,
    ResultadoCaptacaoCidade,
)
from leads.sources import FONTES
from leads.sources.models import BuscaParcialError

pytestmark = pytest.mark.django_db


class _FormularioHTMLParser(HTMLParser):
    """Extrai controles renderizados sem inferir campos ausentes do HTML."""

    def __init__(self):
        super().__init__()
        self.formularios = []
        self._formulario = None
        self._select = None
        self._opcao = None
        self._textarea = None
        self._botao = None

    def handle_starttag(self, tag, attrs):
        atributos = dict(attrs)
        if tag == "form":
            formulario = {"controles": [], "botoes": []}
            self.formularios.append(formulario)
            self._formulario = formulario
        elif self._formulario is None:
            return
        elif tag == "input":
            self._formulario["controles"].append(
                {"tag": tag, "atributos": atributos}
            )
        elif tag == "select":
            self._select = {
                "tag": tag,
                "atributos": atributos,
                "opcoes": [],
            }
            self._formulario["controles"].append(self._select)
        elif tag == "option" and self._select is not None:
            self._opcao = {"atributos": atributos, "texto": ""}
            self._select["opcoes"].append(self._opcao)
        elif tag == "textarea":
            self._textarea = {
                "tag": tag,
                "atributos": atributos,
                "texto": "",
            }
            self._formulario["controles"].append(self._textarea)
        elif tag == "button":
            self._botao = {"atributos": atributos, "texto": ""}
            self._formulario["botoes"].append(self._botao)

    def handle_data(self, data):
        if self._opcao is not None:
            self._opcao["texto"] += data
        if self._textarea is not None:
            self._textarea["texto"] += data
        if self._botao is not None:
            self._botao["texto"] += data

    def handle_endtag(self, tag):
        if tag == "option":
            self._opcao = None
        elif tag == "select":
            self._select = None
        elif tag == "textarea":
            self._textarea = None
        elif tag == "button":
            self._botao = None
        elif tag == "form":
            self._formulario = None


def _analisar_formulario_captacao(html):
    parser = _FormularioHTMLParser()
    parser.feed(html)
    formularios = [
        formulario
        for formulario in parser.formularios
        if any(
            controle["atributos"].get("name") == "nicho"
            for controle in formulario["controles"]
        )
    ]
    assert len(formularios) == 1
    return formularios[0]


def _serializar_controles_sem_botoes(formulario):
    dados = QueryDict("", mutable=True)
    tipos_ignorados = {"button", "file", "image", "reset", "submit"}

    for controle in formulario["controles"]:
        atributos = controle["atributos"]
        nome = atributos.get("name")
        if not nome or "disabled" in atributos:
            continue

        if controle["tag"] == "input":
            tipo = atributos.get("type", "text").lower()
            if tipo in tipos_ignorados:
                continue
            if tipo in {"checkbox", "radio"} and "checked" not in atributos:
                continue
            dados.appendlist(nome, atributos.get("value", ""))
        elif controle["tag"] == "select":
            opcoes = controle["opcoes"]
            selecionadas = [
                opcao for opcao in opcoes if "selected" in opcao["atributos"]
            ]
            if not selecionadas and opcoes and "multiple" not in atributos:
                selecionadas = opcoes[:1]
            for opcao in selecionadas:
                dados.appendlist(
                    nome,
                    opcao["atributos"].get("value", opcao["texto"].strip()),
                )
        elif controle["tag"] == "textarea":
            dados.appendlist(nome, controle["texto"])

    return dados


@pytest.fixture
def superusuario():
    return get_user_model().objects.create_superuser(
        username="admin-captacao",
        password="senha-teste",
    )


@pytest.fixture
def staff():
    return get_user_model().objects.create_user(
        username="staff-captacao",
        password="senha-teste",
        is_staff=True,
    )


@pytest.fixture
def cliente_admin(superusuario):
    cliente = Client()
    cliente.force_login(superusuario)
    return cliente


@pytest.fixture
def motoboys():
    return Nicho.objects.create(codigo="motoboys", nome="Motoboys")


@pytest.fixture
def quadrantes():
    return [
        Quadrante.objects.create(
            cidade="Maringá",
            estado="PR",
            rotulo=rotulo,
            sul=-23.47,
            oeste=-52.03,
            norte=-23.41,
            leste=-51.95,
        )
        for rotulo in ("Q1", "Q2")
    ]


@pytest.fixture
def url_nova_captacao():
    return reverse("admin:leads_varredura_nova_captacao")


def _dados_formulario(**alteracoes):
    dados = {
        "nicho": "",
        "termo": "motoboy",
        "segmento": Segmento.OUTRO,
        "localidade": _localidade("Maringá", "PR"),
        "fonte": "google_places",
        "quadrante": "",
    }
    dados.update(alteracoes)
    return dados


def _localidade(cidade, estado):
    return json.dumps((cidade, estado), ensure_ascii=False, separators=(",", ":"))


def _criar_quadrante(cidade, estado, rotulo="Q1"):
    return Quadrante.objects.create(
        cidade=cidade,
        estado=estado,
        rotulo=rotulo,
        sul=-23.47,
        oeste=-52.03,
        norte=-23.41,
        leste=-51.95,
    )


def _resultado(plano, varreduras=(), **alteracoes):
    dados = {
        "plano": plano,
        "varreduras": tuple(varreduras),
        "total_requisicoes": 4,
        "total_encontrados": 12,
        "total_sem_site": 7,
        "total_novos": 5,
        "total_fechados": 1,
        "franquia_esgotada": "",
        "varredura_com_erro": None,
    }
    dados.update(alteracoes)
    return ResultadoCaptacaoCidade(**dados)


def _visualizar_previa(cliente, url, nicho, **alteracoes):
    dados = _dados_formulario(
        nicho=str(nicho.pk),
        visualizar_plano="1",
        **alteracoes,
    )
    resposta = cliente.post(url, dados)
    assert resposta.status_code == 200
    assert resposta.context["plano"] is not None
    return resposta, dados


def test_botao_so_aparece_para_superusuario(superusuario, staff):
    url_lista = reverse("admin:leads_varredura_changelist")
    cliente = Client()
    cliente.force_login(superusuario)
    assert "Nova Captação" in cliente.get(url_lista).content.decode()

    cliente.force_login(staff)
    assert "Nova Captação" not in cliente.get(url_lista).content.decode()


def test_url_exige_superusuario_e_redireciona_anonimo(
    staff, url_nova_captacao
):
    cliente = Client()
    resposta_anonima = cliente.get(url_nova_captacao)
    assert resposta_anonima.status_code == 302
    assert reverse("admin:login") in resposta_anonima.url

    cliente.force_login(staff)
    assert cliente.get(url_nova_captacao).status_code == 403


def test_get_apenas_exibe_formulario_sem_planejar_ou_executar(
    monkeypatch, cliente_admin, url_nova_captacao
):
    monkeypatch.setattr(
        leads_admin,
        "planejar_captacao_cidade",
        lambda **_kwargs: pytest.fail("GET não pode planejar"),
    )
    monkeypatch.setattr(
        leads_admin,
        "executar_captacao_cidade",
        lambda _plano: pytest.fail("GET não pode executar"),
    )

    resposta = cliente_admin.get(url_nova_captacao)

    assert resposta.status_code == 200
    assert resposta.context["plano"] is None
    assert "Confirmar captação" not in resposta.content.decode()
    assert 'name="confirmar_captacao"' not in resposta.content.decode()


def test_formulario_exibe_apenas_nichos_ativos_outro_e_fontes_do_registro(
    cliente_admin, url_nova_captacao, motoboys, quadrantes
):
    inativo = Nicho.objects.create(
        codigo="nicho-inativo",
        nome="Nicho inativo",
        ativo=False,
    )

    resposta = cliente_admin.get(url_nova_captacao)
    formulario = resposta.context["formulario"]

    assert motoboys in formulario.fields["nicho"].queryset
    assert inativo not in formulario.fields["nicho"].queryset
    assert formulario.fields["segmento"].initial == Segmento.OUTRO
    assert {valor for valor, _rotulo in formulario.fields["fonte"].choices} == set(
        FONTES
    )
    assert "abrangencia" not in formulario.fields
    assert set(formulario.fields) == {
        "nicho",
        "termo",
        "segmento",
        "localidade",
        "fonte",
        "quadrante",
    }
    html = resposta.content.decode()
    assert 'name="localidade"' in html
    assert 'name="cidade"' not in html
    assert 'name="estado"' not in html
    assert (
        formulario.fields["quadrante"].help_text
        == "Deixe vazio para pesquisar todos os quadrantes da cidade."
    )


def test_localidades_distintas_sao_unicas_ordenadas_e_preservam_homonimas(
    settings, cliente_admin, url_nova_captacao, quadrantes
):
    settings.CIDADE_PADRAO = "Maringá"
    settings.ESTADO_PADRAO = "PR"
    _criar_quadrante("Curitiba", "PR")
    _criar_quadrante("Londrina", "PR")
    _criar_quadrante("Maringá", "SC")

    formulario = cliente_admin.get(url_nova_captacao).context["formulario"]
    escolhas = list(formulario.fields["localidade"].choices)

    assert escolhas[0] == ("", "Selecione uma Cidade/UF")
    assert [tuple(json.loads(valor)) for valor, _rotulo in escolhas[1:]] == [
        ("Curitiba", "PR"),
        ("Londrina", "PR"),
        ("Maringá", "PR"),
        ("Maringá", "SC"),
    ]
    assert [rotulo for _valor, rotulo in escolhas[1:]] == [
        "Curitiba/PR",
        "Londrina/PR",
        "Maringá/PR",
        "Maringá/SC",
    ]
    assert formulario.fields["localidade"].initial == _localidade("Maringá", "PR")


def test_localidade_padrao_ausente_nao_cria_opcao_falsa(
    settings, cliente_admin, url_nova_captacao, quadrantes
):
    settings.CIDADE_PADRAO = "Cidade sem grade"
    settings.ESTADO_PADRAO = "ZZ"

    formulario = cliente_admin.get(url_nova_captacao).context["formulario"]
    escolhas = list(formulario.fields["localidade"].choices)

    assert formulario.fields["localidade"].initial is None
    assert _localidade("Cidade sem grade", "ZZ") not in {
        valor for valor, _rotulo in escolhas
    }


def test_sem_grade_exibe_instrucao_e_recusa_planejamento(
    monkeypatch, cliente_admin, url_nova_captacao, motoboys
):
    monkeypatch.setattr(
        leads_admin,
        "planejar_captacao_cidade",
        lambda **_kwargs: pytest.fail("sem grade não pode planejar"),
    )

    resposta_get = cliente_admin.get(url_nova_captacao)
    assert "Nenhuma Cidade/UF com grade cadastrada" in resposta_get.content.decode()

    resposta_post = cliente_admin.post(
        url_nova_captacao,
        _dados_formulario(
            nicho=str(motoboys.pk),
            localidade="",
            visualizar_plano="1",
        ),
    )

    assert resposta_post.status_code == 200
    assert resposta_post.context["plano"] is None
    assert "Nenhuma Cidade/UF possui grade cadastrada" in resposta_post.content.decode()


def test_localidade_adulterada_e_recusada_pelo_formulario(
    monkeypatch, cliente_admin, url_nova_captacao, motoboys, quadrantes
):
    monkeypatch.setattr(
        leads_admin,
        "planejar_captacao_cidade",
        lambda **_kwargs: pytest.fail("localidade adulterada não pode planejar"),
    )

    resposta = cliente_admin.post(
        url_nova_captacao,
        _dados_formulario(
            nicho=str(motoboys.pk),
            localidade=_localidade("Maringá", "SC"),
            visualizar_plano="1",
        ),
    )

    assert resposta.status_code == 200
    assert resposta.context["plano"] is None
    assert "Faça uma escolha válida" in resposta.content.decode()


def test_previa_chama_planejador_com_argumentos_exatos_e_sem_executar(
    monkeypatch, cliente_admin, url_nova_captacao, motoboys, quadrantes
):
    plano_real = leads_admin.planejar_captacao_cidade(
        nicho_codigo="motoboys",
        termo="motoboy",
        segmento=Segmento.OUTRO,
        cidade="Maringá",
        estado="PR",
        fonte="google_places",
    )
    chamadas = []

    def planejar(**kwargs):
        chamadas.append(kwargs)
        return plano_real

    monkeypatch.setattr(leads_admin, "planejar_captacao_cidade", planejar)
    monkeypatch.setattr(
        leads_admin,
        "executar_captacao_cidade",
        lambda _plano: pytest.fail("prévia não pode executar"),
    )

    resposta = cliente_admin.post(
        url_nova_captacao,
        _dados_formulario(
            nicho=str(motoboys.pk),
            termo="  motoboy  ",
            visualizar_plano="1",
        ),
    )

    assert resposta.status_code == 200
    assert chamadas == [
        {
            "nicho_codigo": "motoboys",
            "termo": "motoboy",
            "segmento": Segmento.OUTRO,
            "cidade": "Maringá",
            "estado": "PR",
            "fonte": "google_places",
            "quadrante_rotulo": None,
        }
    ]
    assert Varredura.objects.count() == 0


def test_previa_funciona_sem_credencial_e_mostra_plano_e_custos(
    settings, cliente_admin, url_nova_captacao, motoboys, quadrantes
):
    settings.GOOGLE_PLACES_API_KEY = ""

    resposta, _dados = _visualizar_previa(
        cliente_admin,
        url_nova_captacao,
        motoboys,
    )
    texto = resposta.content.decode()

    assert "Motoboys (motoboys)" in texto
    assert "motoboy em Maringá PR" in texto
    assert "Outro" in texto
    assert "Maringá/PR" in texto
    assert "google_places" in texto
    assert "todos" in texto
    assert "Número de quadrantes" in texto
    assert "Consumo atual" in texto
    assert "Teto mensal" in texto
    assert "Saldo atual" in texto
    assert "até 6 requisição(ões)" in texto
    assert "pode demorar" in texto
    assert Varredura.objects.count() == 0


def test_confirmacao_replaneja_e_executa_somente_o_plano_novo(
    monkeypatch, cliente_admin, url_nova_captacao, motoboys, quadrantes
):
    plano_previa = leads_admin.planejar_captacao_cidade(
        nicho_codigo="motoboys",
        termo="motoboy",
        segmento=Segmento.OUTRO,
        cidade="Maringá",
        estado="PR",
        fonte="google_places",
    )
    plano_novo = SimpleNamespace(**{
        campo: getattr(plano_previa, campo)
        for campo in plano_previa.__dataclass_fields__
    })
    planos = iter((plano_previa, plano_novo))
    chamadas_planejamento = []
    planos_executados = []

    def planejar(**kwargs):
        chamadas_planejamento.append(kwargs)
        return next(planos)

    def executar(plano):
        planos_executados.append(plano)
        return _resultado(plano)

    monkeypatch.setattr(leads_admin, "planejar_captacao_cidade", planejar)
    monkeypatch.setattr(leads_admin, "executar_captacao_cidade", executar)
    previa, dados = _visualizar_previa(
        cliente_admin,
        url_nova_captacao,
        motoboys,
    )
    dados.update(
        confirmar_captacao="1",
        assinatura_previa=previa.context["assinatura_previa"],
    )
    dados.pop("visualizar_plano")

    resposta = cliente_admin.post(url_nova_captacao, dados)

    assert resposta.status_code == 302
    assert resposta.url == reverse("admin:leads_varredura_changelist")
    assert chamadas_planejamento == [chamadas_planejamento[0]] * 2
    assert chamadas_planejamento[1]["cidade"] == "Maringá"
    assert chamadas_planejamento[1]["estado"] == "PR"
    assert planos_executados == [plano_novo]
    assert planos_executados[0] is not plano_previa


def test_html_serializa_confirmacao_e_executa_plano_novo_uma_vez(
    monkeypatch, cliente_admin, url_nova_captacao, motoboys, quadrantes
):
    plano_previa = leads_admin.planejar_captacao_cidade(
        nicho_codigo="motoboys",
        termo="motoboy",
        segmento=Segmento.OUTRO,
        cidade="Maringá",
        estado="PR",
        fonte="google_places",
    )
    plano_novo = SimpleNamespace(**{
        campo: getattr(plano_previa, campo)
        for campo in plano_previa.__dataclass_fields__
    })
    planos = iter((plano_previa, plano_novo))
    chamadas_planejamento = []
    planos_executados = []
    confirmacoes = []

    def planejar(**kwargs):
        chamadas_planejamento.append(kwargs)
        return next(planos)

    def executar(plano):
        planos_executados.append(plano)
        return _resultado(plano)

    monkeypatch.setattr(leads_admin, "planejar_captacao_cidade", planejar)
    monkeypatch.setattr(leads_admin, "executar_captacao_cidade", executar)
    confirmar_original = VarreduraAdmin._confirmar_captacao

    def confirmar(painel, request, formulario):
        confirmacoes.append(True)
        return confirmar_original(painel, request, formulario)

    monkeypatch.setattr(VarreduraAdmin, "_confirmar_captacao", confirmar)

    formulario_inicial = _analisar_formulario_captacao(
        cliente_admin.get(url_nova_captacao).content.decode()
    )
    assert not any(
        controle["atributos"].get("name") == "confirmar_captacao"
        for controle in formulario_inicial["controles"]
    )

    previa = cliente_admin.post(
        url_nova_captacao,
        _dados_formulario(nicho=str(motoboys.pk), visualizar_plano="1"),
    )
    formulario_confirmacao = _analisar_formulario_captacao(
        previa.content.decode()
    )
    controles_confirmacao = [
        controle
        for controle in formulario_confirmacao["controles"]
        if controle["atributos"].get("name") == "confirmar_captacao"
    ]
    assert controles_confirmacao == [
        {
            "tag": "input",
            "atributos": {
                "type": "hidden",
                "name": "confirmar_captacao",
                "value": "1",
            },
        }
    ]
    botoes_confirmacao = [
        botao
        for botao in formulario_confirmacao["botoes"]
        if " ".join(botao["texto"].split()) == "Confirmar captação"
    ]
    assert len(botoes_confirmacao) == 1
    assert "name" not in botoes_confirmacao[0]["atributos"]

    dados_confirmacao = _serializar_controles_sem_botoes(formulario_confirmacao)
    assert dados_confirmacao.getlist("confirmar_captacao") == ["1"]
    assert dados_confirmacao.getlist("localidade") == [_localidade("Maringá", "PR")]
    assert "visualizar_plano" not in dados_confirmacao

    resposta = cliente_admin.post(url_nova_captacao, dados_confirmacao)

    assert confirmacoes == [True]
    assert resposta.status_code == 302
    assert resposta.url == reverse("admin:leads_varredura_changelist")
    assert len(chamadas_planejamento) == 2
    assert planos_executados == [plano_novo]


def test_payload_alterado_e_assinatura_expirada_exigem_nova_previa(
    monkeypatch, cliente_admin, url_nova_captacao, motoboys, quadrantes
):
    executar = []
    monkeypatch.setattr(
        leads_admin,
        "executar_captacao_cidade",
        lambda plano: executar.append(plano),
    )
    previa, dados = _visualizar_previa(
        cliente_admin,
        url_nova_captacao,
        motoboys,
    )
    payload = leads_admin.signing.loads(
        previa.context["assinatura_previa"],
        salt=leads_admin._CAPTACAO_ASSINATURA_SALT,
        max_age=leads_admin._CAPTACAO_ASSINATURA_MAX_AGE,
    )
    assert set(payload) == {
        "nicho",
        "termo",
        "segmento",
        "cidade",
        "estado",
        "fonte",
        "quadrante",
        "nonce",
    }
    assert "localidade" not in payload
    assert payload["cidade"] == "Maringá"
    assert payload["estado"] == "PR"
    assert all(isinstance(valor, str) for valor in payload.values())

    somente_assinatura = {
        **dados,
        "assinatura_previa": previa.context["assinatura_previa"],
    }
    somente_assinatura.pop("visualizar_plano")
    sem_acao = cliente_admin.post(url_nova_captacao, somente_assinatura)
    assert sem_acao.status_code == 200
    assert executar == []

    dados_assinatura_alterada = {
        **dados,
        "confirmar_captacao": "1",
        "assinatura_previa": previa.context["assinatura_previa"] + "x",
    }
    dados_assinatura_alterada.pop("visualizar_plano")
    assinatura_alterada = cliente_admin.post(
        url_nova_captacao,
        dados_assinatura_alterada,
    )
    assert assinatura_alterada.status_code == 200
    assert "prévia é inválida" in assinatura_alterada.content.decode()
    assert executar == []

    dados.update(
        termo="entrega",
        confirmar_captacao="1",
        assinatura_previa=previa.context["assinatura_previa"],
    )
    dados.pop("visualizar_plano")

    alterada = cliente_admin.post(url_nova_captacao, dados)
    assert alterada.status_code == 200
    assert "dados foram alterados" in alterada.content.decode()
    assert executar == []

    previa, dados = _visualizar_previa(
        cliente_admin,
        url_nova_captacao,
        motoboys,
    )
    dados.update(
        confirmar_captacao="1",
        assinatura_previa=previa.context["assinatura_previa"],
    )
    dados.pop("visualizar_plano")
    monkeypatch.setattr(leads_admin, "_CAPTACAO_ASSINATURA_MAX_AGE", -1)

    expirada = cliente_admin.post(url_nova_captacao, dados)
    assert expirada.status_code == 200
    assert "prévia expirou" in expirada.content.decode()
    assert executar == []


def test_nonce_e_de_uso_unico(
    monkeypatch, cliente_admin, url_nova_captacao, motoboys, quadrantes
):
    eventos = []
    consumir_nonce_original = VarreduraAdmin._consumir_nonce

    def consumir_nonce(request, nonce):
        consumido = consumir_nonce_original(request, nonce)
        assert nonce not in request.session.get(
            leads_admin._CAPTACAO_NONCES_SESSION_KEY,
            (),
        )
        eventos.append("nonce_consumido")
        return consumido

    def executar(plano):
        eventos.append("execucao_iniciada")
        return _resultado(plano)

    monkeypatch.setattr(
        VarreduraAdmin,
        "_consumir_nonce",
        staticmethod(consumir_nonce),
    )
    monkeypatch.setattr(leads_admin, "executar_captacao_cidade", executar)
    previa, dados = _visualizar_previa(
        cliente_admin,
        url_nova_captacao,
        motoboys,
    )
    dados.update(
        confirmar_captacao="1",
        assinatura_previa=previa.context["assinatura_previa"],
    )
    dados.pop("visualizar_plano")

    assert cliente_admin.post(url_nova_captacao, dados).status_code == 302
    reutilizada = cliente_admin.post(url_nova_captacao, dados)

    assert reutilizada.status_code == 200
    assert "já foi usada" in reutilizada.content.decode()
    assert eventos == [
        "nonce_consumido",
        "execucao_iniciada",
        "nonce_consumido",
    ]


def test_post_sem_csrf_e_recusado(
    superusuario, url_nova_captacao, motoboys
):
    cliente = Client(enforce_csrf_checks=True)
    cliente.force_login(superusuario)

    resposta = cliente.post(
        url_nova_captacao,
        _dados_formulario(nicho=str(motoboys.pk), visualizar_plano="1"),
    )

    assert resposta.status_code == 403


def test_sucesso_redireciona_mostra_totais_e_interrupcao_por_franquia(
    monkeypatch, cliente_admin, url_nova_captacao, motoboys, quadrantes
):
    monkeypatch.setattr(
        leads_admin,
        "executar_captacao_cidade",
        lambda plano: _resultado(plano),
    )
    previa, dados = _visualizar_previa(
        cliente_admin,
        url_nova_captacao,
        motoboys,
    )
    dados.update(
        confirmar_captacao="1",
        assinatura_previa=previa.context["assinatura_previa"],
    )
    dados.pop("visualizar_plano")

    resposta = cliente_admin.post(url_nova_captacao, dados, follow=True)
    mensagens = list(get_messages(resposta.wsgi_request))

    assert resposta.redirect_chain == [
        (reverse("admin:leads_varredura_changelist"), 302)
    ]
    assert len(mensagens) == 1
    assert mensagens[0].level == SUCCESS
    assert "Captação concluída" in str(mensagens[0])
    assert "4 requisição(ões)" in str(mensagens[0])
    assert "5 prospect(s) novo(s)" in str(mensagens[0])

    def executar_interrompida(plano):
        return _resultado(
            plano,
            franquia_esgotada="Franquia mensal esgotada para google_places.",
        )

    monkeypatch.setattr(
        leads_admin,
        "executar_captacao_cidade",
        executar_interrompida,
    )
    previa, dados = _visualizar_previa(
        cliente_admin,
        url_nova_captacao,
        motoboys,
    )
    dados.update(
        confirmar_captacao="1",
        assinatura_previa=previa.context["assinatura_previa"],
    )
    dados.pop("visualizar_plano")

    resposta = cliente_admin.post(url_nova_captacao, dados, follow=True)
    mensagens = list(get_messages(resposta.wsgi_request))

    assert resposta.redirect_chain == [
        (reverse("admin:leads_varredura_changelist"), 302)
    ]
    assert len(mensagens) == 1
    assert mensagens[0].level == WARNING
    assert "Captação interrompida por franquia" in str(mensagens[0])
    assert "Captação concluída" not in str(mensagens[0])
    assert "4 requisição(ões)" in str(mensagens[0])
    assert "5 prospect(s) novo(s)" in str(mensagens[0])


def test_falha_parcial_mostra_somente_resultado_publico_seguro(
    monkeypatch, cliente_admin, url_nova_captacao, motoboys, quadrantes, caplog
):
    segredo = "credencial-nao-pode-aparecer"

    class BuscaParcialSensivel(BuscaParcialError):
        def __str__(self):
            return segredo

    def executar(plano):
        varredura = Varredura.objects.create(
            nicho=motoboys,
            segmento=Segmento.OUTRO,
            termo_busca=plano.consulta,
            fonte="GOOGLE_PLACES",
            cidade="Maringá",
            estado="PR",
            quadrante=quadrantes[0],
        )
        resultado = _resultado(
            plano,
            (varredura,),
            varredura_com_erro=varredura,
        )
        raise CaptacaoCidadeParcialError(
            resultado=resultado,
            causa=BuscaParcialSensivel(candidatos=[], total_requisicoes=2),
            quadrante=quadrantes[0],
        )

    monkeypatch.setattr(leads_admin, "executar_captacao_cidade", executar)
    previa, dados = _visualizar_previa(
        cliente_admin,
        url_nova_captacao,
        motoboys,
    )
    dados.update(
        confirmar_captacao="1",
        assinatura_previa=previa.context["assinatura_previa"],
    )
    dados.pop("visualizar_plano")

    resposta = cliente_admin.post(url_nova_captacao, dados, follow=True)
    mensagens_objetos = list(get_messages(resposta.wsgi_request))
    mensagens = " ".join(str(mensagem) for mensagem in mensagens_objetos)
    url_lista = reverse("admin:leads_varredura_changelist")
    varredura = Varredura.objects.get()

    assert resposta.status_code == 200
    assert resposta.redirect_chain == [(url_lista, 302)]
    assert resposta.request["PATH_INFO"] == url_lista
    assert [mensagem.level for mensagem in mensagens_objetos] == [WARNING, ERROR]
    assert "Quadrantes registrados: Q1" in mensagens
    assert "foi registrada com ERRO" in mensagens
    assert "seguintes não foram executados" in mensagens
    assert segredo not in mensagens
    assert segredo not in caplog.text
    assert "evento=captacao_admin_falha_parcial" in caplog.text
    assert "causa_tipo=BuscaParcialSensivel" in caplog.text
    assert f"varredura_id={varredura.pk}" in caplog.text
    assert f"varreduras_registradas_ids={varredura.pk}" in caplog.text
    assert "quadrante=Q1" in caplog.text


def test_falha_de_finalizacao_preserva_so_resultados_seguros_e_nao_afirma_erro(
    monkeypatch, cliente_admin, url_nova_captacao, motoboys, quadrantes, caplog
):
    segredo_operacional = "token-operacional-secreto"
    segredo_finalizacao = "senha-banco-secreta"
    referencias = {}

    def executar(plano):
        segura = Varredura.objects.create(
            nicho=motoboys,
            segmento=Segmento.OUTRO,
            termo_busca=plano.consulta,
            fonte="GOOGLE_PLACES",
            cidade="Maringá",
            estado="PR",
            quadrante=quadrantes[0],
        )
        incerta = Varredura(
            nicho=motoboys,
            segmento=Segmento.OUTRO,
            termo_busca=plano.consulta,
            fonte="GOOGLE_PLACES",
            cidade="Maringá",
            estado="PR",
            quadrante=quadrantes[1],
        )
        referencias["segura"] = segura.pk
        referencias["incerta"] = incerta.pk
        raise CaptacaoCidadeFalhaFinalizacaoError(
            resultado=_resultado(plano, (segura,)),
            varredura=incerta,
            causa_operacional=RuntimeError(segredo_operacional),
            causa_finalizacao=RuntimeError(segredo_finalizacao),
            quadrante=quadrantes[1],
            busca_parcial=False,
        )

    monkeypatch.setattr(leads_admin, "executar_captacao_cidade", executar)
    previa, dados = _visualizar_previa(
        cliente_admin,
        url_nova_captacao,
        motoboys,
    )
    dados.update(
        confirmar_captacao="1",
        assinatura_previa=previa.context["assinatura_previa"],
    )
    dados.pop("visualizar_plano")

    resposta = cliente_admin.post(url_nova_captacao, dados, follow=True)
    mensagens_objetos = list(get_messages(resposta.wsgi_request))
    mensagens = " ".join(str(mensagem) for mensagem in mensagens_objetos)
    url_lista = reverse("admin:leads_varredura_changelist")

    assert resposta.status_code == 200
    assert resposta.redirect_chain == [(url_lista, 302)]
    assert resposta.request["PATH_INFO"] == url_lista
    assert [mensagem.level for mensagem in mensagens_objetos] == [WARNING, ERROR]
    assert "Resultado preservado: 1 Varredura" in mensagens
    assert "não foi possível persistir o encerramento" in mensagens.lower()
    assert "status ERRO não está confirmado" in mensagens
    assert "foi registrada com ERRO" not in mensagens
    assert segredo_operacional not in mensagens
    assert segredo_finalizacao not in mensagens
    assert segredo_operacional not in caplog.text
    assert segredo_finalizacao not in caplog.text
    assert "evento=captacao_admin_falha_finalizacao" in caplog.text
    assert "causa_operacional_tipo=RuntimeError" in caplog.text
    assert "causa_finalizacao_tipo=RuntimeError" in caplog.text
    assert f"varredura_id={referencias['incerta']}" in caplog.text
    assert f"varreduras_seguras_ids={referencias['segura']}" in caplog.text
    assert "quadrante=Q2" in caplog.text


def test_erro_de_dominio_na_previa_mantem_formulario(
    monkeypatch, cliente_admin, url_nova_captacao, motoboys, quadrantes
):
    monkeypatch.setattr(
        leads_admin,
        "planejar_captacao_cidade",
        lambda **_kwargs: (_ for _ in ()).throw(
            CaptacaoCidadeError("Nenhum quadrante para Cidade/UF.")
        ),
    )

    resposta = cliente_admin.post(
        url_nova_captacao,
        _dados_formulario(nicho=str(motoboys.pk), visualizar_plano="1"),
    )

    assert resposta.status_code == 200
    assert "Nenhum quadrante" in resposta.content.decode()
    assert resposta.context["formulario"].data["termo"] == "motoboy"


def test_erro_de_preflight_na_confirmacao_redireciona_e_exige_nova_previa(
    monkeypatch, cliente_admin, url_nova_captacao, motoboys, quadrantes, caplog
):
    segredo = "token-preflight-nao-pode-aparecer"
    monkeypatch.setattr(
        leads_admin,
        "executar_captacao_cidade",
        lambda _plano: (_ for _ in ()).throw(
            CaptacaoCidadeError(segredo)
        ),
    )
    previa, dados = _visualizar_previa(
        cliente_admin,
        url_nova_captacao,
        motoboys,
    )
    dados.update(
        confirmar_captacao="1",
        assinatura_previa=previa.context["assinatura_previa"],
    )
    dados.pop("visualizar_plano")

    resposta = cliente_admin.post(url_nova_captacao, dados, follow=True)
    mensagens_objetos = list(get_messages(resposta.wsgi_request))
    mensagens = " ".join(str(mensagem) for mensagem in mensagens_objetos)
    html = resposta.content.decode()
    url_lista = reverse("admin:leads_varredura_changelist")

    assert resposta.status_code == 200
    assert resposta.redirect_chain == [(url_lista, 302)]
    assert resposta.request["PATH_INFO"] == url_lista
    assert [mensagem.level for mensagem in mensagens_objetos] == [ERROR]
    assert "não foi iniciada" in mensagens
    assert "Visualize um novo plano" in mensagens
    assert segredo not in mensagens
    assert segredo not in html
    assert segredo not in caplog.text
    assert "evento=captacao_admin_preflight_recusado" in caplog.text
    assert "excecao_tipo=CaptacaoCidadeError" in caplog.text
    assert "fase=preflight" in caplog.text
    assert Varredura.objects.count() == 0


def test_nicho_desativado_e_grade_alterada_entre_previa_e_confirmacao_sao_recusados(
    monkeypatch, cliente_admin, url_nova_captacao, motoboys, quadrantes
):
    monkeypatch.setattr(
        leads_admin,
        "executar_captacao_cidade",
        lambda _plano: pytest.fail("não deveria executar"),
    )
    previa, dados = _visualizar_previa(
        cliente_admin,
        url_nova_captacao,
        motoboys,
    )
    motoboys.ativo = False
    motoboys.save(update_fields=["ativo"])
    dados.update(
        confirmar_captacao="1",
        assinatura_previa=previa.context["assinatura_previa"],
    )
    dados.pop("visualizar_plano")

    resposta = cliente_admin.post(url_nova_captacao, dados)
    assert resposta.status_code == 200
    assert "não são mais válidos" in resposta.content.decode()

    motoboys.ativo = True
    motoboys.save(update_fields=["ativo"])
    previa, dados = _visualizar_previa(
        cliente_admin,
        url_nova_captacao,
        motoboys,
        quadrante="Q2",
    )
    quadrantes[1].delete()
    dados.update(
        confirmar_captacao="1",
        assinatura_previa=previa.context["assinatura_previa"],
    )
    dados.pop("visualizar_plano")

    resposta = cliente_admin.post(url_nova_captacao, dados)
    assert resposta.status_code == 200
    assert "Quadrante &#x27;Q2&#x27; não existe" in resposta.content.decode()
    assert "Visualize um novo plano" in resposta.content.decode()


def test_localidade_removida_entre_previa_e_confirmacao_e_recusada(
    monkeypatch, cliente_admin, url_nova_captacao, motoboys, quadrantes
):
    monkeypatch.setattr(
        leads_admin,
        "executar_captacao_cidade",
        lambda _plano: pytest.fail("localidade removida não pode executar"),
    )
    previa, dados = _visualizar_previa(
        cliente_admin,
        url_nova_captacao,
        motoboys,
    )
    Quadrante.objects.filter(cidade="Maringá", estado="PR").delete()
    dados.update(
        confirmar_captacao="1",
        assinatura_previa=previa.context["assinatura_previa"],
    )
    dados.pop("visualizar_plano")

    resposta = cliente_admin.post(url_nova_captacao, dados)
    html = resposta.content.decode()

    assert resposta.status_code == 200
    assert "Faça uma escolha válida" in html
    assert "dados da captação não são mais válidos" in html
    assert "Visualize um novo plano" in html


def test_varredura_admin_exibe_e_filtra_nicho_sem_ocultar_nulo(
    cliente_admin, motoboys, quadrantes
):
    com_nicho = Varredura.objects.create(
        termo_busca="motoboy em Maringá PR",
        segmento=Segmento.OUTRO,
        nicho=motoboys,
        cidade="Maringá",
        estado="PR",
        quadrante=quadrantes[0],
    )
    sem_nicho = Varredura.objects.create(
        termo_busca="operação interna",
        segmento=Segmento.OUTRO,
        nicho=None,
        cidade="Maringá",
        estado="PR",
        quadrante=quadrantes[1],
    )
    painel = VarreduraAdmin(Varredura, django_admin.site)
    assert "nicho" in painel.list_display
    assert "nicho" in painel.list_filter

    url = reverse("admin:leads_varredura_changelist")
    resposta = cliente_admin.get(url)
    ids = set(resposta.context["cl"].queryset.values_list("pk", flat=True))
    assert ids == {com_nicho.pk, sem_nicho.pk}

    filtrada = cliente_admin.get(url, {"nicho__id__exact": motoboys.pk})
    assert list(filtrada.context["cl"].queryset) == [com_nicho]


def test_admin_nao_importa_management_command_e_erros_inesperados_propagam(
    monkeypatch, cliente_admin, url_nova_captacao, motoboys, quadrantes
):
    assert "management.commands" not in inspect.getsource(leads_admin)
    previa, dados = _visualizar_previa(
        cliente_admin,
        url_nova_captacao,
        motoboys,
    )
    dados.update(
        confirmar_captacao="1",
        assinatura_previa=previa.context["assinatura_previa"],
    )
    dados.pop("visualizar_plano")
    monkeypatch.setattr(
        leads_admin,
        "executar_captacao_cidade",
        lambda _plano: (_ for _ in ()).throw(RuntimeError("defeito")),
    )

    with pytest.raises(RuntimeError, match="defeito"):
        cliente_admin.post(url_nova_captacao, dados)
