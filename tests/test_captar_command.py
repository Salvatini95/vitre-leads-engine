"""Contrato da captação por cidade compartilhada entre CLI e futuro Admin."""

from __future__ import annotations

from io import StringIO
from types import SimpleNamespace

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from leads.management.commands import captar as captacao_cidade_command
from leads.models import Nicho, Quadrante, Segmento, StatusVarredura, Varredura
from leads.services import captacao, captacao_cidade
from leads.sources.models import BuscaParcialError

pytestmark = pytest.mark.django_db


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
        for rotulo in ("Q1", "Q2", "Q3")
    ]


def _executar_cli_esperando_erro(*argumentos: str) -> str:
    """Executa a entrada CLI real e combina stdout/stderr na ordem emitida."""
    saida = StringIO()
    comando = captacao_cidade_command.Command(
        stdout=saida,
        stderr=saida,
        no_color=True,
    )

    with pytest.raises(SystemExit) as info:
        comando.run_from_argv(["manage.py", "captar", *argumentos])

    assert info.value.code == 1
    return saida.getvalue()


def test_modo_novo_usa_nicho_termo_e_segmento_outro(motoboys, quadrantes):
    saida = StringIO()

    call_command(
        "captar",
        nicho="motoboys",
        termo="motoboy",
        dry_run=True,
        stdout=saida,
    )

    texto = saida.getvalue()
    assert "Nicho: Motoboys (motoboys)" in texto
    assert "Segmento: Outro" in texto
    assert "Consulta: motoboy em Maringá PR" in texto
    assert Varredura.objects.count() == 0


def test_modo_novo_preserva_segmento_explicito(
    monkeypatch, motoboys, quadrantes, settings
):
    saida = StringIO()
    chamadas = []
    settings.GOOGLE_PLACES_API_KEY = "chave-teste"

    def varredura_falsa(**kwargs):
        chamadas.append(kwargs)
        return SimpleNamespace(
            quadrante=kwargs["quadrante"],
            total_requisicoes=0,
            total_encontrados=0,
            total_sem_site=0,
            total_novos=0,
            total_fechados=0,
        )

    monkeypatch.setattr(captacao_cidade, "executar_varredura", varredura_falsa)

    call_command(
        "captar",
        nicho="motoboys",
        termo="entrega rápida",
        segmento=Segmento.BARBEARIA,
        quadrante="Q2",
        stdout=saida,
    )

    assert "Segmento: Barbearia" in saida.getvalue()
    assert len(chamadas) == 1
    assert chamadas[0]["nicho"] == motoboys
    assert chamadas[0]["segmento"] == Segmento.BARBEARIA
    assert chamadas[0]["consulta"] == "entrega rápida em Maringá PR"
    assert chamadas[0]["quadrante"].rotulo == "Q2"


def test_modo_novo_transmite_outro_e_contrato_completo_para_execucao(
    monkeypatch, motoboys, quadrantes, settings
):
    chamadas = []
    settings.GOOGLE_PLACES_API_KEY = "chave-teste"

    def varredura_falsa(**kwargs):
        chamadas.append(kwargs)
        return SimpleNamespace(
            quadrante=kwargs["quadrante"],
            total_requisicoes=0,
            total_encontrados=0,
            total_sem_site=0,
            total_novos=0,
            total_fechados=0,
        )

    monkeypatch.setattr(captacao_cidade, "executar_varredura", varredura_falsa)

    call_command(
        "captar",
        nicho="motoboys",
        termo="motoboy",
        quadrante="Q3",
        stdout=StringIO(),
    )

    assert len(chamadas) == 1
    assert chamadas[0] == {
        "nicho": motoboys,
        "segmento": Segmento.OUTRO,
        "consulta": "motoboy em Maringá PR",
        "cidade": "Maringá",
        "estado": "PR",
        "quadrante": quadrantes[2],
        "fonte": "google_places",
    }


@pytest.mark.parametrize(
    "argumentos,mensagem",
    [
        ({"nicho": "motoboys"}, "--nicho e --termo devem ser informados juntos"),
        ({"termo": "motoboy"}, "--nicho e --termo devem ser informados juntos"),
        (
            {"nicho": "motoboys", "termo": "   "},
            "O termo da atividade não pode ser vazio",
        ),
    ],
)
def test_argumentos_novos_invalidos_falham_com_erro_claro(
    argumentos, mensagem, motoboys, quadrantes
):
    with pytest.raises(CommandError, match=mensagem):
        call_command("captar", **argumentos)

    assert Varredura.objects.count() == 0


def test_nicho_inexistente_falha_antes_de_varredura_ou_api(monkeypatch, quadrantes):
    monkeypatch.setattr(
        captacao_cidade,
        "executar_varredura",
        lambda **_kwargs: pytest.fail("não deveria executar varredura"),
    )

    with pytest.raises(CommandError, match="Nicho 'inexistente' não existe"):
        call_command("captar", nicho="inexistente", termo="motoboy")

    assert Varredura.objects.count() == 0


def test_nicho_inativo_falha_antes_de_varredura_ou_api(
    monkeypatch, motoboys, quadrantes
):
    motoboys.ativo = False
    motoboys.save(update_fields=["ativo"])
    monkeypatch.setattr(
        captacao_cidade,
        "executar_varredura",
        lambda **_kwargs: pytest.fail("não deveria executar varredura"),
    )

    with pytest.raises(CommandError, match="Nicho 'motoboys' está inativo"):
        call_command("captar", nicho="motoboys", termo="motoboy")

    assert Varredura.objects.count() == 0


@pytest.mark.parametrize("estado_nicho", ["ausente", "inativo"])
def test_modo_legado_exige_beleza_ativo(estado_nicho, quadrantes):
    beleza = Nicho.objects.get(codigo="beleza")
    if estado_nicho == "ausente":
        beleza.delete()
        mensagem = "Nicho 'beleza' não existe"
    else:
        beleza.ativo = False
        beleza.save(update_fields=["ativo"])
        mensagem = "Nicho 'beleza' está inativo"

    with pytest.raises(CommandError, match=mensagem):
        call_command("captar", segmento=Segmento.SALAO, dry_run=True)


def test_modo_legado_continua_usando_beleza_e_label_do_segmento(quadrantes):
    saida = StringIO()

    call_command("captar", segmento=Segmento.SALAO, dry_run=True, stdout=saida)

    texto = saida.getvalue()
    assert "Segmento: Salão de beleza" in texto
    assert "Nicho:" not in texto

    nicho, termo, segmento = captacao_cidade_command.Command._contrato_canonico(
        {"nicho": None, "termo": None, "segmento": Segmento.SALAO}
    )
    assert nicho == "beleza"
    assert termo == Segmento.SALAO.label
    assert segmento == Segmento.SALAO


def test_comandos_legados_continuam_validos(monkeypatch, quadrantes, settings):
    chamadas = []
    settings.GOOGLE_PLACES_API_KEY = "chave-teste"

    def varredura_falsa(**kwargs):
        chamadas.append(kwargs)
        return SimpleNamespace(
            quadrante=kwargs["quadrante"],
            total_requisicoes=0,
            total_encontrados=0,
            total_sem_site=0,
            total_novos=0,
            total_fechados=0,
        )

    monkeypatch.setattr(captacao_cidade, "executar_varredura", varredura_falsa)

    call_command("captar", segmento=Segmento.SALAO, stdout=StringIO())
    call_command(
        "captar",
        segmento=Segmento.NAIL,
        quadrante="Q3",
        stdout=StringIO(),
    )

    assert len(chamadas) == 4


def test_modo_legado_transmite_contrato_completo_ao_servico(
    monkeypatch, quadrantes, settings
):
    chamadas = []
    settings.FOURSQUARE_API_KEY = "chave-teste"

    def varredura_falsa(**kwargs):
        chamadas.append(kwargs)
        return SimpleNamespace(
            quadrante=kwargs["quadrante"],
            total_requisicoes=0,
            total_encontrados=0,
            total_sem_site=0,
            total_novos=0,
            total_fechados=0,
        )

    monkeypatch.setattr(captacao_cidade, "executar_varredura", varredura_falsa)

    call_command(
        "captar",
        segmento=Segmento.NAIL,
        quadrante="Q3",
        fonte="foursquare",
        stdout=StringIO(),
    )

    assert len(chamadas) == 1
    assert chamadas[0] == {
        "nicho": Nicho.objects.get(codigo="beleza"),
        "segmento": Segmento.NAIL,
        "consulta": "Nail designer em Maringá PR",
        "cidade": "Maringá",
        "estado": "PR",
        "quadrante": quadrantes[2],
        "fonte": "foursquare",
    }
    call_command(
        "captar",
        segmento=Segmento.SALAO,
        fonte="foursquare",
        dry_run=True,
        stdout=StringIO(),
    )


def test_dry_run_valida_plano_sem_executar_ou_criar_varredura(
    monkeypatch, motoboys, quadrantes, settings
):
    settings.GOOGLE_PLACES_API_KEY = ""
    monkeypatch.setattr(
        captacao_cidade,
        "executar_varredura",
        lambda **_kwargs: pytest.fail("dry-run não pode executar"),
    )

    call_command(
        "captar",
        nicho="motoboys",
        termo="motoboy",
        dry_run=True,
        stdout=StringIO(),
    )

    assert Varredura.objects.count() == 0


def test_planejamento_seleciona_todos_os_quadrantes_e_calcula_franquia_por_fonte(
    motoboys, quadrantes, settings
):
    settings.FOURSQUARE_FRANQUIA_MENSAL = 100
    Varredura.objects.create(
        termo_busca="histórico",
        segmento=Segmento.OUTRO,
        nicho=motoboys,
        fonte="FOURSQUARE",
        cidade="Maringá",
        estado="PR",
        total_requisicoes=7,
    )

    plano = captacao_cidade.planejar_captacao_cidade(
        nicho_codigo="motoboys",
        termo="motoboy",
        segmento=Segmento.OUTRO,
        cidade="Maringá",
        estado="PR",
        fonte="foursquare",
    )

    assert plano.quadrantes == tuple(quadrantes)
    assert plano.consulta == "motoboy em Maringá PR"
    assert isinstance(plano.quadrantes, tuple)
    assert plano.estimativa_requisicoes == 9
    assert plano.consumo_franquia == 7
    assert plano.teto_franquia == 100
    assert plano.saldo_franquia == 93


def test_planejamento_filtra_quadrante_especifico(motoboys, quadrantes):
    plano = captacao_cidade.planejar_captacao_cidade(
        nicho_codigo="motoboys",
        termo="motoboy",
        segmento=Segmento.OUTRO,
        cidade="Maringá",
        estado="PR",
        fonte="google_places",
        quadrante_rotulo="Q3",
    )

    assert [quadrante.rotulo for quadrante in plano.quadrantes] == ["Q3"]


def test_cidade_sem_grade_falha_com_erro_claro(motoboys, quadrantes):
    with pytest.raises(captacao_cidade.CaptacaoCidadeError, match="Nenhum quadrante"):
        captacao_cidade.planejar_captacao_cidade(
            nicho_codigo="motoboys",
            termo="motoboy",
            segmento=Segmento.OUTRO,
            cidade="Londrina",
            estado="PR",
            fonte="google_places",
        )


@pytest.mark.parametrize(
    "campo,valor,mensagem",
    [
        ("segmento", "INVALIDO", "Segmento inválido"),
        ("fonte", "waze", "Fonte desconhecida"),
    ],
)
def test_planejamento_publico_rejeita_segmento_e_fonte_invalidos(
    campo, valor, mensagem, motoboys, quadrantes
):
    argumentos = {
        "nicho_codigo": "motoboys",
        "termo": "motoboy",
        "segmento": Segmento.OUTRO,
        "cidade": "Maringá",
        "estado": "PR",
        "fonte": "google_places",
    }
    argumentos[campo] = valor

    with pytest.raises(captacao_cidade.CaptacaoCidadeError, match=mensagem):
        captacao_cidade.planejar_captacao_cidade(**argumentos)


def test_quadrante_inexistente_falha_com_erro_claro(motoboys, quadrantes):
    with pytest.raises(captacao_cidade.CaptacaoCidadeError, match="Quadrante 'Q9'"):
        captacao_cidade.planejar_captacao_cidade(
            nicho_codigo="motoboys",
            termo="motoboy",
            segmento=Segmento.OUTRO,
            cidade="Maringá",
            estado="PR",
            fonte="google_places",
            quadrante_rotulo="Q9",
        )


def test_servico_executa_plano_agrega_resultados_e_nao_escreve_stdout(
    monkeypatch, capsys, motoboys, quadrantes, settings
):
    settings.GOOGLE_PLACES_API_KEY = "chave-teste"
    plano = captacao_cidade.planejar_captacao_cidade(
        nicho_codigo="motoboys",
        termo="motoboy",
        segmento=Segmento.OUTRO,
        cidade="Maringá",
        estado="PR",
        fonte="google_places",
    )

    def varredura_falsa(**kwargs):
        return SimpleNamespace(
            quadrante=kwargs["quadrante"],
            total_requisicoes=1,
            total_encontrados=4,
            total_sem_site=3,
            total_novos=2,
            total_fechados=1,
        )

    monkeypatch.setattr(captacao_cidade, "executar_varredura", varredura_falsa)

    resultado = captacao_cidade.executar_captacao_cidade(plano)

    assert len(resultado.varreduras) == 3
    assert resultado.total_requisicoes == 3
    assert resultado.total_encontrados == 12
    assert resultado.total_sem_site == 9
    assert resultado.total_novos == 6
    assert resultado.total_fechados == 3
    assert resultado.franquia_esgotada == ""
    assert resultado.varredura_com_erro is None
    assert capsys.readouterr().out == ""


def test_servico_interrompe_com_seguranca_quando_franquia_acaba(
    monkeypatch, motoboys, quadrantes, settings
):
    settings.GOOGLE_PLACES_API_KEY = "chave-teste"
    settings.PLACES_FRANQUIA_MENSAL = 1
    plano = captacao_cidade.planejar_captacao_cidade(
        nicho_codigo="motoboys",
        termo="motoboy",
        segmento=Segmento.OUTRO,
        cidade="Maringá",
        estado="PR",
        fonte="google_places",
    )

    async def coleta_falsa(_consulta, _celula, _fonte, _segmento):
        return captacao.Coleta(
            candidatos=[],
            requisicoes=1,
            vereditos=[],
        )

    monkeypatch.setattr(captacao, "_coletar", coleta_falsa)

    resultado = captacao_cidade.executar_captacao_cidade(plano)

    assert len(resultado.varreduras) == 1
    assert resultado.total_requisicoes == 1
    assert "Franquia mensal esgotada" in resultado.franquia_esgotada
    assert Varredura.objects.count() == 1


@pytest.mark.parametrize(
    "fonte,setting_invalido,valor_invalido",
    [
        ("google_places", "GOOGLE_PLACES_API_KEY", None),
        ("google_places", "GOOGLE_PLACES_API_KEY", "   "),
        ("foursquare", "FOURSQUARE_API_KEY", None),
        ("foursquare", "FOURSQUARE_API_BASE", None),
        ("foursquare", "FOURSQUARE_API_BASE", ""),
        ("foursquare", "FOURSQUARE_API_BASE", "   "),
    ],
)
def test_preflight_com_configuracao_ausente_falha_antes_de_varredura_ou_rede(
    monkeypatch,
    settings,
    fonte,
    setting_invalido,
    valor_invalido,
    motoboys,
    quadrantes,
):
    setattr(settings, setting_invalido, valor_invalido)
    plano = captacao_cidade.planejar_captacao_cidade(
        nicho_codigo="motoboys",
        termo="motoboy",
        segmento=Segmento.OUTRO,
        cidade="Maringá",
        estado="PR",
        fonte=fonte,
    )
    monkeypatch.setattr(
        captacao_cidade,
        "executar_varredura",
        lambda **_kwargs: pytest.fail("não deveria criar Varredura nem chamar fonte"),
    )

    with pytest.raises(captacao_cidade.CaptacaoCidadeError, match=setting_invalido):
        captacao_cidade.executar_captacao_cidade(plano)

    assert Varredura.objects.count() == 0


def test_erro_inesperado_depois_da_criacao_finaliza_varredura_como_erro(
    monkeypatch, settings, motoboys, quadrantes
):
    settings.GOOGLE_PLACES_API_KEY = "chave-teste"
    plano = captacao_cidade.planejar_captacao_cidade(
        nicho_codigo="motoboys",
        termo="motoboy",
        segmento=Segmento.OUTRO,
        cidade="Maringá",
        estado="PR",
        fonte="google_places",
        quadrante_rotulo="Q1",
    )

    async def coleta_com_erro(*_args):
        raise RuntimeError("falha inesperada")

    monkeypatch.setattr(captacao, "_coletar", coleta_com_erro)

    with pytest.raises(RuntimeError, match="falha inesperada"):
        captacao_cidade.executar_captacao_cidade(plano)

    varredura = Varredura.objects.get()
    assert varredura.status == StatusVarredura.ERRO
    assert varredura.erro == "falha inesperada"
    assert varredura.concluido_em is not None


def _simular_falha_parcial_no_segundo_quadrante(monkeypatch):
    executados = []

    async def coleta_falsa(_consulta, celula, _fonte, _segmento):
        executados.append(celula.rotulo)
        if celula.rotulo == "Q1":
            return captacao.Coleta(candidatos=[], requisicoes=1, vereditos=[])
        if celula.rotulo == "Q2":
            raise BuscaParcialError(total_requisicoes=2, candidatos=[])
        pytest.fail("Q3 não deveria ser executado")

    monkeypatch.setattr(captacao, "_coletar", coleta_falsa)
    return executados


def _simular_falha_de_finalizacao_no_segundo_quadrante(monkeypatch):
    executados = []
    causa_operacional = RuntimeError("falha operacional com dado sigiloso")
    causa_finalizacao = RuntimeError("falha ao salvar estado")

    async def coleta_falsa(_consulta, celula, _fonte, _segmento):
        executados.append(celula.rotulo)
        if celula.rotulo == "Q1":
            return captacao.Coleta(candidatos=[], requisicoes=1, vereditos=[])
        if celula.rotulo == "Q2":
            raise causa_operacional
        pytest.fail("Q3 não deveria ser executado")

    save_original = Varredura.save

    def save_com_falha_no_erro(self, *args, **kwargs):
        if self.status == StatusVarredura.ERRO:
            raise causa_finalizacao
        return save_original(self, *args, **kwargs)

    monkeypatch.setattr(captacao, "_coletar", coleta_falsa)
    monkeypatch.setattr(Varredura, "save", save_com_falha_no_erro)
    return executados, causa_operacional, causa_finalizacao


def test_falha_parcial_carrega_resultado_e_varredura_que_falhou(
    monkeypatch, settings, motoboys, quadrantes
):
    settings.GOOGLE_PLACES_API_KEY = "chave-teste"
    executados = _simular_falha_parcial_no_segundo_quadrante(monkeypatch)
    plano = captacao_cidade.planejar_captacao_cidade(
        nicho_codigo="motoboys",
        termo="motoboy",
        segmento=Segmento.OUTRO,
        cidade="Maringá",
        estado="PR",
        fonte="google_places",
    )

    with pytest.raises(captacao_cidade.CaptacaoCidadeParcialError) as info:
        captacao_cidade.executar_captacao_cidade(plano)

    erro = info.value
    resultado = erro.resultado
    persistidas = {
        varredura.quadrante.rotulo: varredura
        for varredura in Varredura.objects.select_related("quadrante")
    }
    q1_persistida = persistidas["Q1"]
    q2_persistida = persistidas["Q2"]
    rotulos_resultado = [v.quadrante.rotulo for v in resultado.varreduras]
    assert executados == ["Q1", "Q2"]
    assert rotulos_resultado == ["Q1", "Q2"]
    assert rotulos_resultado.count("Q1") == 1
    assert rotulos_resultado.count("Q2") == 1
    assert resultado.varredura_com_erro.quadrante.rotulo == "Q2"
    assert resultado.varredura_com_erro.pk == q2_persistida.pk
    assert q1_persistida.status == StatusVarredura.CONCLUIDA
    assert q2_persistida.status == StatusVarredura.ERRO
    assert q2_persistida.total_requisicoes == 2
    assert q2_persistida.erro
    assert q2_persistida.concluido_em is not None
    assert resultado.total_requisicoes == 3
    assert erro.quadrante.rotulo == "Q2"
    assert isinstance(erro.causa, BuscaParcialError)
    assert isinstance(erro.__cause__, captacao.VarreduraParcialError)
    assert set(persistidas) == {"Q1", "Q2"}
    assert not Varredura.objects.filter(quadrante=quadrantes[2]).exists()


def test_cli_imprime_varreduras_parciais_antes_de_encerrar_com_erro(
    monkeypatch, settings, motoboys, quadrantes
):
    settings.GOOGLE_PLACES_API_KEY = "chave-teste"
    executados = _simular_falha_parcial_no_segundo_quadrante(monkeypatch)
    texto = _executar_cli_esperando_erro(
        "--nicho",
        "motoboys",
        "--termo",
        "motoboy",
    )

    assert executados == ["Q1", "Q2"]
    assert texto.index("Q1:") < texto.index("Q2: ERRO")
    assert texto.index("Q2: ERRO") < texto.index("CommandError:")
    assert texto.count("Q1:") == 1
    assert texto.count("Q2: ERRO") == 1
    assert "2 req" in texto
    assert Varredura.objects.count() == 2


def test_servico_preserva_resultados_anteriores_se_finalizacao_falha(
    monkeypatch, settings, motoboys, quadrantes
):
    settings.GOOGLE_PLACES_API_KEY = "chave-teste"
    executados, causa_operacional, causa_finalizacao = (
        _simular_falha_de_finalizacao_no_segundo_quadrante(monkeypatch)
    )
    plano = captacao_cidade.planejar_captacao_cidade(
        nicho_codigo="motoboys",
        termo="motoboy",
        segmento=Segmento.OUTRO,
        cidade="Maringá",
        estado="PR",
        fonte="google_places",
    )

    with pytest.raises(captacao_cidade.CaptacaoCidadeFalhaFinalizacaoError) as info:
        captacao_cidade.executar_captacao_cidade(plano)

    erro = info.value
    persistidas = {
        varredura.quadrante.rotulo: varredura
        for varredura in Varredura.objects.select_related("quadrante")
    }
    assert executados == ["Q1", "Q2"]
    assert [v.quadrante.rotulo for v in erro.resultado.varreduras] == ["Q1"]
    assert erro.varredura.quadrante.rotulo == "Q2"
    assert erro.causa_operacional is causa_operacional
    assert erro.causa_finalizacao is causa_finalizacao
    assert erro.busca_parcial is False
    assert erro.estado_erro_persistido is False
    assert isinstance(erro.__cause__, captacao.FalhaFinalizacaoVarreduraError)
    assert erro.__cause__.__cause__ is causa_finalizacao
    assert persistidas["Q1"].status == StatusVarredura.CONCLUIDA
    assert persistidas["Q2"].status == StatusVarredura.RODANDO
    assert "sigiloso" not in str(erro)
    assert set(persistidas) == {"Q1", "Q2"}
    assert not Varredura.objects.filter(quadrante=quadrantes[2]).exists()


def test_cli_imprime_resultados_anteriores_quando_finalizacao_falha(
    monkeypatch, settings, motoboys, quadrantes
):
    settings.GOOGLE_PLACES_API_KEY = "chave-teste"
    executados, _causa_operacional, _causa_finalizacao = (
        _simular_falha_de_finalizacao_no_segundo_quadrante(monkeypatch)
    )

    texto = _executar_cli_esperando_erro(
        "--nicho",
        "motoboys",
        "--termo",
        "motoboy",
    )

    assert executados == ["Q1", "Q2"]
    assert texto.count("Q1:") == 1
    assert texto.index("Q1:") < texto.index("CommandError:")
    assert "não foi possível registrar o estado de erro" in texto
    assert "sigiloso" not in texto
    assert not Varredura.objects.filter(quadrante=quadrantes[2]).exists()
