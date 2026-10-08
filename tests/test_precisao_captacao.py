"""Precisão configurável: parâmetros reais, filtros e persistência sem rede externa."""
from io import StringIO
from types import SimpleNamespace

import httpx
import pytest
import respx
from django.core.management import call_command

from leads.filters.site_validator import SiteVerdict
from leads.models import Nicho, Prospect, Quadrante, Segmento, Varredura
from leads.services import captacao, captacao_cidade
from leads.services.opcoes_captacao import OpcoesCaptacao, corresponde_ao_perfil
from leads.sources.foursquare import FoursquareSource, descrever_busca
from leads.sources.models import ProspectCandidate, ResultadoBusca

pytestmark = pytest.mark.django_db
ENDPOINT = "https://places-api.foursquare.com/places/search"


def candidato(nome="Cred Fácil", categoria="Loan Agency", telefone="+5547999277416", id="p1"):
    return ProspectCandidate(origem="FOURSQUARE", origem_id=id, nome=nome,
                             categoria=categoria, telefone=telefone)


@pytest.fixture
def cenario():
    nicho = Nicho.objects.create(codigo="correspondentes_bancarios", nome="Correspondentes bancários")
    q = Quadrante.objects.create(cidade="Joinville", estado="SC", rotulo="Q1",
                                sul=-26.4, norte=-26.1, oeste=-49.2, leste=-48.8)
    return nicho, q


def planejar(**extra):
    return captacao_cidade.planejar_captacao_cidade(
        nicho_codigo="correspondentes_bancarios", termo="crédito automotivo",
        segmento=Segmento.OUTRO, cidade="Joinville", estado="SC", fonte="foursquare", **extra)


@pytest.mark.asyncio
@pytest.mark.parametrize("modo,tem_categoria,tem_query", [
    ("automatico", True, False), ("texto", False, True), ("categoria_texto", True, True),
])
async def test_foursquare_envia_modo_escolhido(modo, tem_categoria, tem_query):
    with respx.mock:
        rota = respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json={"results": []}))
        async with FoursquareSource("chave-ficticia") as fonte:
            await fonte.buscar("crédito automotivo", segmento="OUTRO",
                               nicho_codigo="correspondentes_bancarios", modo_busca=modo)
        params = rota.calls[0].request.url.params
        assert ("fsq_category_ids" in params) is tem_categoria
        assert ("query" in params) is tem_query
        if tem_query:
            assert params["query"] == "crédito automotivo"
        assert "tel" in params["fields"].split(",")


@pytest.mark.asyncio
async def test_categoria_texto_sem_mapeamento_recusa_antes_de_http():
    with respx.mock:
        async with FoursquareSource("chave-ficticia") as fonte:
            with pytest.raises(ValueError, match="mapeado"):
                await fonte.buscar("loja", segmento="OUTRO", modo_busca="categoria_texto")


@pytest.mark.asyncio
async def test_categoria_texto_preserva_parametros_na_paginacao():
    with respx.mock:
        rota = respx.get(ENDPOINT).mock(side_effect=[
            httpx.Response(200, json={"results": []}, headers={"Link": f'<{ENDPOINT}?cursor=abc>; rel="next"'}),
            httpx.Response(200, json={"results": []}),
        ])
        async with FoursquareSource("chave-ficticia") as fonte:
            resultado = await fonte.buscar("crédito", nicho_codigo="correspondentes_bancarios", modo_busca="categoria_texto")
        assert resultado.total_requisicoes == 2
        assert rota.calls[1].request.url.params["cursor"] == "abc"
        for chamada in rota.calls:
            assert chamada.request.url.params["query"] == "crédito"
            assert chamada.request.url.params["fsq_category_ids"]


@pytest.mark.parametrize("perfil,nome,categoria,esperado", [
    ("financeiro", "Cred Fácil", "Loan Agency", True),
    ("financeiro", "Assessoria de Financiamento", "Office", True),
    ("financeiro", "Cred Burger", "Hamburger Restaurant", False),
    ("financeiro", "Salão Crédito", "Hair Salon", False),
    ("financeiro", "Sicredi", "Bank", False),
    ("financeiro", "Loja Aleatória", "Gift Shop", False),
    ("financeiro", "FGV MBA em Gestão Financeira", "School", False),
    ("financeiro", "Aula Administração Financeira", "Education", False),
    ("financeiro", "Aula de Avaliação Financeira", "Office", False),
    ("financeiro", "Pilar Assessoria Contábil e Financeira", "Accounting", False),
    ("financeiro", "Segefi - Secretaria de Gestão Financeira", "Government", False),
    ("financeiro", "4Cred Soluções Financeiras", "Office", True),
    ("financeiro", "Roma Financeira", "Office", True),
    ("financeiro", "Promovva - Sua casa de Crédito", "Office", True),
    ("financeiro", "FGV MBA em Gestão Financeira", "School", False),
    ("financeiro", "Aula Administração Financeira", "Education", False),
    ("financeiro", "Aula de Avaliação Financeira", "Office", False),
    ("financeiro", "Pilar Assessoria Contábil e Financeira", "Accounting", False),
    ("financeiro", "Segefi - Secretaria de Gestão Financeira", "Government", False),
    ("financeiro", "4Cred Soluções Financeiras", "Office", True),
    ("financeiro", "Roma Financeira", "Office", True),
    ("financeiro", "Promovva - Sua casa de Crédito", "Office", True),
    ("veiculos", "Silva Automóveis", "Car Dealership", True),
    ("veiculos", "Silva", "Used Car Dealer", True),
    ("veiculos", "Revenda de Veículos", "Store", True),
    ("veiculos", "Casa do Hambúrguer", "Restaurant", False),
    ("veiculos", "Oficina Mecânica", "Auto Repair", False),
    ("veiculos", "Autopeças Silva", "Auto Parts", False),
])
def test_perfil_usa_indicios_e_veta_categorias_irrelevantes(perfil, nome, categoria, esperado):
    assert corresponde_ao_perfil(candidato(nome, categoria), perfil) is esperado


def test_planejamento_explica_termo_ignorado_e_preserva_default(cenario):
    plano = planejar()
    assert "NÃO enviado" in plano.descricao_busca
    assert plano.opcoes.argumentos() == {}
    assert plano.incluir_com_site is False


@pytest.mark.parametrize("extra", [
    {"modo_busca": "invalido"}, {"perfil_resultados": "invalido"},
    {"incluir_com_site": "False"}, {"somente_celular": 1},
])
def test_opcoes_invalidas_nao_criam_varredura(cenario, extra):
    with pytest.raises(captacao_cidade.CaptacaoCidadeError):
        planejar(**extra)
    assert Varredura.objects.count() == 0


def test_google_recusa_modo_foursquare(cenario):
    with pytest.raises(ValueError, match="exclusivos"):
        OpcoesCaptacao(modo_busca="texto").validar("google_places")


def test_execucao_transmite_termo_sem_cidade_e_opcoes(cenario, monkeypatch, settings):
    settings.FOURSQUARE_API_KEY = "chave-ficticia"
    plano = planejar(modo_busca="texto", perfil_resultados="veiculos", incluir_com_site=True, somente_celular=True)
    chamadas = []
    def executar(**kwargs):
        chamadas.append(kwargs)
        return SimpleNamespace(total_requisicoes=1,total_encontrados=1,total_sem_site=0,total_novos=1,total_fechados=0)
    monkeypatch.setattr(captacao_cidade, "executar_varredura", executar)
    captacao_cidade.executar_captacao_cidade(plano)
    assert chamadas[0]["consulta"] == "crédito automotivo em Joinville SC"
    assert chamadas[0]["termo"] == "crédito automotivo"
    assert chamadas[0]["modo_busca"] == "texto"
    assert chamadas[0]["perfil_resultados"] == "veiculos"
    assert chamadas[0]["incluir_com_site"] is True
    assert chamadas[0]["somente_celular"] is True


@pytest.mark.asyncio
async def test_coleta_filtro_veiculos_substitui_filtro_do_nicho_e_celular(monkeypatch):
    itens = [candidato("Silva Automóveis", "Car Dealer", id="p1"),
             candidato("Cred Burger", "Burger Restaurant", id="p2"),
             candidato("Revenda Souza", "Car Dealer", telefone="+554734513900", id="p3")]
    class Fonte:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def buscar(self, *args, **kwargs):
            assert args[0] == "automóveis"
            assert kwargs == {"modo_busca": "texto"}
            return ResultadoBusca(candidatos=itens, total_requisicoes=2)
    class Validador:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def validar(self, url): return SiteVerdict(tem_site_real=False, evidencia="teste")
    monkeypatch.setattr(captacao, "criar_fonte", lambda fonte: Fonte())
    monkeypatch.setattr(captacao, "SiteValidator", Validador)
    resultado = await captacao._coletar("automóveis em Joinville SC", None, "foursquare", "OUTRO",
        "correspondentes_bancarios", modo_busca="texto", perfil_resultados="veiculos", somente_celular=True, termo="automóveis")
    assert [c.origem_id for c in resultado.candidatos] == ["p1"]
    assert resultado.requisicoes == 2  # Filtros locais não devolvem o custo de rede.


@pytest.mark.parametrize("incluir,novos", [(False, 0), (True, 1)])
def test_incluir_site_e_opt_in_sem_qualificar_automaticamente(cenario, incluir, novos):
    nicho, q = cenario
    v = Varredura.objects.create(nicho=nicho, segmento="OUTRO", cidade="Joinville",estado="SC",termo_busca="crédito",quadrante=q)
    verdict = SiteVerdict(tem_site_real=True,evidencia="site ativo")
    sem_site, criados = captacao._persistir(v, [candidato()], [verdict], site_confiavel=False, incluir_com_site=incluir)
    assert sem_site == 0
    assert criados == novos
    if incluir:
        p = Prospect.objects.get()
        assert p.tem_site_real is True
        assert p.ativo_no_funil is False
        assert p.e_alvo is False


def test_recaptura_com_site_preserva_nicho_status_manual_e_dedup(cenario):
    nicho, q = cenario
    v = Varredura.objects.create(nicho=nicho,segmento="OUTRO",cidade="Joinville",estado="SC",termo_busca="crédito",quadrante=q)
    sem = SiteVerdict(tem_site_real=False,evidencia="teste")
    com = SiteVerdict(tem_site_real=True,evidencia="site ativo")
    captacao._persistir(v,[candidato()],[sem],site_confiavel=False)
    p = Prospect.objects.get();p.status_funil="EM_ANDAMENTO";p.revisado_manualmente=True;p.save()
    outro = Nicho.objects.create(codigo="outra-campanha",nome="Outra");v.nicho=outro;v.save()
    assert captacao._persistir(v,[candidato()],[com],site_confiavel=False,incluir_com_site=True) == (0,0)
    p.refresh_from_db()
    assert p.nicho_id == nicho.pk
    assert p.status_funil == "EM_ANDAMENTO"
    assert Prospect.objects.count() == 1


def test_cli_dry_run_descreve_busca_sem_rede(cenario):
    saida = StringIO()
    call_command("captar",nicho="correspondentes_bancarios",termo="veículos",segmento="OUTRO",cidade="Joinville",estado="SC",
                 fonte="foursquare",modo_busca="texto",perfil_resultados="veiculos",incluir_com_site=True,dry_run=True,stdout=saida)
    assert "Termo enviado: veículos; sem categoria automática" in saida.getvalue()
    assert "incluir com site: True" in saida.getvalue()
    assert Varredura.objects.count() == 0
