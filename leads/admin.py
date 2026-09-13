"""Admin — a tela de trabalho diária: curadoria, funil e follow-up.

O Admin do Django faz aqui o papel do painel: filtro por segmento e status,
edição em lote e busca. É de propósito denso e sem enfeite — a ferramenta é
usada várias vezes por dia e o que importa é velocidade.
"""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta
from urllib.parse import quote

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.widgets import AdminSplitDateTime
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.db.models import BooleanField, Case, QuerySet, Value, When
from django.http import HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html

from leads.forms import CaptacaoCidadeForm
from leads.models import (
    Canal,
    Descarte,
    Interacao,
    Nicho,
    OrigemLocalizacao,
    Prospect,
    ProspectVerificacao,
    Quadrante,
    Socio,
    Segmento,
    StatusFunil,
    Varredura,
)
from leads.services.captacao_cidade import (
    CaptacaoCidadeError,
    CaptacaoCidadeFalhaFinalizacaoError,
    CaptacaoCidadeParcialError,
    ResultadoCaptacaoCidade,
    executar_captacao_cidade,
    planejar_captacao_cidade,
)
from leads.utils.telefone import TipoTelefone


logger = logging.getLogger(__name__)

_CAPTACAO_ASSINATURA_SALT = "leads.admin.nova_captacao.v1"
_CAPTACAO_ASSINATURA_MAX_AGE = 15 * 60
_CAPTACAO_NONCES_SESSION_KEY = "leads_nova_captacao_nonces"
_CAPTACAO_NONCES_MAXIMOS = 10


class SocioInline(admin.TabularInline):
    model = Socio
    extra = 0


class InteracaoInline(admin.TabularInline):
    model = Interacao
    extra = 1
    readonly_fields = ("criado_em",)


class DescarteInline(admin.TabularInline):
    model = Descarte
    extra = 0
    readonly_fields = ("criado_em",)


_CAMPOS_LOCALIZACAO_FACTUAL = frozenset(
    {
        "endereco",
        "bairro",
        "cidade",
        "estado",
        "pais",
        "cep",
        "latitude",
        "longitude",
    }
)


class OrigemLocalizacaoAdminMixin:
    """Marca como manual somente uma edição geográfica feita no Admin."""

    def save_model(self, request, obj, form, change):
        if _CAMPOS_LOCALIZACAO_FACTUAL.intersection(form.changed_data):
            obj.origem_localizacao = OrigemLocalizacao.MANUAL
        super().save_model(request, obj, form, change)


@admin.register(Nicho)
class NichoAdmin(admin.ModelAdmin):
    list_display = ("nome", "codigo", "ativo")
    list_filter = ("ativo",)
    search_fields = ("nome", "codigo")

    def get_readonly_fields(self, request, obj=None):
        # O código identifica o nicho em dados e integrações; não muda depois
        # de criado para continuar estável.
        return ("codigo",) if obj else ()


@admin.register(Prospect)
class ProspectAdmin(OrigemLocalizacaoAdminMixin, admin.ModelAdmin):
    list_display = (
        "nome",
        "segmento",
        "status_funil",
        "tag_verificacao",
        "telefone",
        "site_evidencia",
        "total_avaliacoes",
        "rating",
        "revisado_manualmente",
    )
    list_filter = (
        "status_funil",
        "segmento",
        "nicho",
        "tem_site_real",
        "revisado_manualmente",
        "cidade",
        "origem",
    )
    search_fields = ("nome", "razao_social", "nome_fantasia", "telefone", "cnpj")
    list_editable = ("segmento", "status_funil")
    readonly_fields = ("origem", "origem_id", "criado_em", "atualizado_em", "varredura")
    inlines = (SocioInline, InteracaoInline, DescarteInline)
    # Mais avaliações primeiro: negócio consolidado e sem site é a melhor porta.
    ordering = ("-total_avaliacoes",)
    actions = ("aprovar_para_funil", "marcar_revisado")

    @admin.display(description="Verificação")
    def tag_verificacao(self, obj: Prospect) -> str:
        return obj.tag_verificacao or "—"

    @admin.action(description="Aprovar para o funil (NOVO → A iniciar)")
    def aprovar_para_funil(self, request, queryset):
        # A trava: só NOVO sobe ao funil. VERIFICAR_SITE fica de fora por
        # construção — nenhum prospect cuja fonte não sabe dizer se há site
        # entra no funil sem alguém checar. "Selecionar tudo → aprovar" era
        # exatamente o caminho pelo qual os 373 palpites da Foursquare
        # subiriam junto com prospect qualificado de verdade.
        bloqueados = queryset.filter(status_funil=StatusFunil.VERIFICAR_SITE).count()

        atualizados = queryset.filter(status_funil=StatusFunil.NOVO).update(
            status_funil=StatusFunil.INICIAR,
            ativo_no_funil=True,
            revisado_manualmente=True,
            atualizado_em=timezone.now(),
        )
        self.message_user(
            request,
            f"{atualizados} prospect(s) enviados ao funil.",
            messages.SUCCESS,
        )

        if bloqueados:
            self.message_user(
                request,
                f"{bloqueados} prospect(s) NÃO foram aprovados: estão pendentes "
                "de verificação de site. Confira em "
                "«Fila de verificação de site» e confirme um a um.",
                messages.WARNING,
            )

    @admin.action(description="Marcar como revisado (trava a sobrescrita)")
    def marcar_revisado(self, request, queryset):
        atualizados = queryset.update(revisado_manualmente=True)
        self.message_user(request, f"{atualizados} marcado(s) como revisado.")


@admin.register(ProspectVerificacao)
class ProspectVerificacaoAdmin(OrigemLocalizacaoAdminMixin, admin.ModelAdmin):
    """Fila de verificação manual de site.

    Tela separada de propósito: o `ProspectAdmin` continua servindo o fluxo
    do Google exatamente como antes, e esta fila tem regra própria.

    Ordenação por TELEFONE primeiro, não por avaliações. A Foursquare não dá
    nota nem contagem de avaliação sem campo pago (`FOURSQUARE_CAMPOS_PRO`
    segue False), então `-total_avaliacoes` ordenaria 400 nulos — ordem
    aleatória na prática. Telefone é o único sinal disponível de que vale
    gastar revisão manual: sem telefone não há como abordar, mesmo que o lead
    se confirme.

    Mas "tem telefone" aqui é FORMATO DE CELULAR, não campo preenchido. Os
    158 preenchidos de Maringá continham 101 fixos: ordenar por campo não
    vazio colocava no topo da fila uma centena de números que não abrem
    conversa por mensagem, que é o único canal da abordagem. O critério
    passou a ser `telefone_tipo == CELULAR`.

    O que a coluna NÃO diz: que o número atende ou que tem WhatsApp. Isso só
    se confirma contatando a linha, e o protocolo não faz contato automático.
    """

    list_display = (
        "nome",
        "tem_celular",
        "telefone",
        "segmento_editavel",
        "abrir_whatsapp",
        "telefone_tipo",
        "tag_verificacao",
        "endereco",
        "website_url",
        "site_evidencia",
    )
    list_filter = ("telefone_tipo", "origem", "segmento", "cidade")
    search_fields = ("nome", "telefone", "endereco")
    readonly_fields = ("origem", "origem_id", "criado_em", "atualizado_em", "varredura")
    actions = ("confirmar_sem_site", "descartar_tem_site")

    @admin.display(description="Verificação")
    def tag_verificacao(self, obj: Prospect) -> str:
        return obj.tag_verificacao

    @admin.display(description="Celular?", boolean=True, ordering="_tem_celular")
    def tem_celular(self, obj: Prospect) -> bool:
        """Formato de celular válido. Não afirma que a linha tem WhatsApp."""
        return obj.telefone_e_celular

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "<path:object_id>/whatsapp/",
                self.admin_site.admin_view(self.abrir_whatsapp_view),
                name="leads_prospectverificacao_whatsapp",
            ),
            path(
                "<path:object_id>/segmento/",
                self.admin_site.admin_view(self.alterar_segmento_view),
                name="leads_prospectverificacao_segmento",
            ),
        ]
        return custom_urls + urls

    @admin.display(description="Segmento", ordering="segmento")
    def segmento_editavel(self, obj: Prospect):
        action = reverse(
            "admin:leads_prospectverificacao_segmento",
            args=[obj.pk],
        )

        options = []

        for value, label in Segmento.choices:
            url = f"{action}?segmento={value}"
            selected = " selected" if obj.segmento == value else ""

            options.append(
                f'<option value="{url}"{selected}>{label}</option>'
            )

        return format_html(
            '<select onchange="window.location.href=this.value;" '
            'style="min-width:150px;padding:4px 6px;">{}</select>',
            format_html("".join(options)),
        )

    def alterar_segmento_view(self, request, object_id):
        obj = self.get_object(request, object_id)

        if obj is None:
            self.message_user(
                request,
                "Prospect não encontrado.",
                messages.ERROR,
            )
            return HttpResponseRedirect(
                reverse("admin:leads_prospectverificacao_changelist")
            )

        novo_segmento = request.GET.get("segmento", "")
        segmentos_validos = {value for value, _ in Segmento.choices}

        if novo_segmento not in segmentos_validos:
            self.message_user(
                request,
                "Segmento inválido.",
                messages.ERROR,
            )
            return HttpResponseRedirect(
                reverse("admin:leads_prospectverificacao_changelist")
            )

        obj.segmento = novo_segmento
        obj.revisado_manualmente = True
        obj.atualizado_em = timezone.now()

        obj.save(
            update_fields=[
                "segmento",
                "revisado_manualmente",
                "atualizado_em",
            ]
        )

        self.message_user(
            request,
            f"Segmento de {obj.nome} atualizado para "
            f"{obj.get_segmento_display()}.",
            messages.SUCCESS,
        )

        return HttpResponseRedirect(
            reverse("admin:leads_prospectverificacao_changelist")
        )

    def _mensagem_whatsapp(self, obj: Prospect) -> str:
        if obj.segmento == Segmento.BARBEARIA:
            return """Oi! Tudo bem? Sou o Guilherme, desenvolvedor aqui de Maringá.

Encontrei a barbearia de vocês pesquisando negócios da região e curti bastante a proposta.

Estou desenvolvendo alguns modelos de sites voltados para barbearias, para apresentar melhor os serviços, equipe, trabalhos e facilitar o contato ou agendamento.

Separei esse exemplo para te mostrar a ideia. Posso te mandar o link também?"""

        if obj.segmento in {
            Segmento.SALAO,
            Segmento.ESTETICA,
            Segmento.NAIL,
            Segmento.LASH,
            Segmento.SOBRANCELHA,
        }:
            return """Oi! Tudo bem? Sou o Guilherme, desenvolvedor aqui de Maringá.

Encontrei vocês pesquisando negócios da área de beleza da região e achei o trabalho de vocês bem interessante.

Estou desenvolvendo alguns modelos de sites para negócios locais, pensados para apresentar melhor os serviços, facilitar o contato e também ajudar quem procura a empresa pelo Google.

Separei esse exemplo para te mostrar a ideia. Posso te mandar o link também?"""

        return """Oi! Tudo bem? Sou o Guilherme, desenvolvedor aqui de Maringá.

Encontrei vocês pesquisando negócios da região e achei interessante o trabalho da empresa.

Estou desenvolvendo alguns modelos de sites para negócios locais, pensados para apresentar melhor os serviços, facilitar o contato e ajudar quem procura a empresa pelo Google.

Separei um exemplo para te mostrar a ideia. Posso te mandar o link também?"""

    def _url_whatsapp(self, obj: Prospect) -> str:
        numero = "".join(filter(str.isdigit, obj.telefone))
        mensagem = self._mensagem_whatsapp(obj)
        return f"https://wa.me/{numero}?text={quote(mensagem)}"

    @admin.display(description="WhatsApp")
    def abrir_whatsapp(self, obj: Prospect):
        if not obj.telefone or not obj.telefone_e_celular:
            return "—"

        url = reverse(
            "admin:leads_prospectverificacao_whatsapp",
            args=[obj.pk],
        )

        return format_html(
            '<a href="{}" target="_blank" '
            'style="background:#198754;color:white;padding:6px 10px;'
            'border-radius:4px;text-decoration:none;white-space:nowrap;" '
            'onclick="return confirm(\'Confirma que este prospect não tem site e deseja iniciar a abordagem pelo WhatsApp?\');">'
            'Abrir WhatsApp</a>',
            url,
        )

    def abrir_whatsapp_view(self, request, object_id):
        obj = self.get_object(request, object_id)

        if obj is None:
            self.message_user(
                request,
                "Prospect não encontrado ou já removido da fila.",
                messages.WARNING,
            )
            return HttpResponseRedirect(
                reverse("admin:leads_prospectverificacao_changelist")
            )

        if not obj.telefone or not obj.telefone_e_celular:
            self.message_user(
                request,
                "Este prospect não possui celular válido para abordagem.",
                messages.ERROR,
            )
            return HttpResponseRedirect(
                reverse("admin:leads_prospectverificacao_changelist")
            )

        whatsapp_url = self._url_whatsapp(obj)

        # A fila contém apenas VERIFICAR_SITE. Ao iniciar a abordagem,
        # registramos uma única interação e movemos o prospect para o funil.
        if obj.status_funil == StatusFunil.VERIFICAR_SITE:
            Interacao.objects.create(
                prospect=obj,
                canal=Canal.WHATSAPP,
                anotacao=(
                    "Primeira abordagem iniciada via WhatsApp pelo painel. "
                    "Mensagem pré-preenchida aberta para envio manual."
                ),
            )

            agora = timezone.now()

            obj.status_funil = StatusFunil.EM_ANDAMENTO
            obj.ativo_no_funil = True
            obj.tem_site_real = False
            obj.site_evidencia = "verificado à mão: sem site próprio"
            obj.revisado_manualmente = True

            if not obj.proximo_contato_em:
                obj.proximo_contato_em = agora + timedelta(days=2)

            obj.atualizado_em = agora

            obj.save(
                update_fields=[
                    "status_funil",
                    "ativo_no_funil",
                    "tem_site_real",
                    "site_evidencia",
                    "revisado_manualmente",
                    "proximo_contato_em",
                    "atualizado_em",
                ]
            )

        return HttpResponseRedirect(whatsapp_url)

    def get_queryset(self, request) -> QuerySet[ProspectVerificacao]:
        # Sem `super()` de propósito: `ModelAdmin.get_queryset` já aplica
        # `order_by` antes de qualquer anotação existir, e ordenar por
        # `_tem_celular` ali estoura FieldError. Aqui a ordem é anotar →
        # ordenar.
        qs = self.model._default_manager.get_queryset().filter(
            status_funil=StatusFunil.VERIFICAR_SITE
        )
        qs = qs.annotate(
            _tem_celular=Case(
                When(telefone_tipo=TipoTelefone.CELULAR, then=Value(True)),
                default=Value(False),
                output_field=BooleanField(),
            )
        )
        return qs.order_by(*self.get_ordering(request))

    def get_ordering(self, request) -> tuple[str, ...]:
        # Em `get_ordering` e não no atributo `ordering` porque o system check
        # admin.E033 valida `ordering` contra campos do modelo, e
        # `_tem_celular` é anotação.
        return ("-_tem_celular", "nome")

    def has_add_permission(self, request) -> bool:
        # A fila nasce da captação, nunca da mão.
        return False

    @admin.action(description="Confirmei: NÃO tem site → mandar para curadoria")
    def confirmar_sem_site(self, request, queryset):
        """Saída aprovada da fila: vira prospect normal em curadoria.

        Não pula direto para o funil de propósito — o prospect volta ao mesmo
        ponto em que um lead do Google nasce (NOVO), e segue o mesmo caminho
        de curadoria a partir dali.
        """
        atualizados = queryset.update(
            status_funil=StatusFunil.NOVO,
            tem_site_real=False,
            site_evidencia="verificado à mão: sem site próprio",
            revisado_manualmente=True,
            atualizado_em=timezone.now(),
        )
        self.message_user(
            request,
            f"{atualizados} confirmado(s) sem site e enviado(s) à curadoria.",
            messages.SUCCESS,
        )

    @admin.action(description="Verifiquei: TEM site → descartar")
    def descartar_tem_site(self, request, queryset):
        atualizados = queryset.update(
            status_funil=StatusFunil.DESCARTADO,
            ativo_no_funil=False,
            tem_site_real=True,
            site_evidencia="verificado à mão: tem site próprio",
            revisado_manualmente=True,
            atualizado_em=timezone.now(),
        )
        self.message_user(
            request,
            f"{atualizados} descartado(s) por ter site próprio.",
            messages.SUCCESS,
        )


@admin.register(Varredura)
class VarreduraAdmin(admin.ModelAdmin):
    change_list_template = "admin/leads/varredura/change_list.html"
    list_display = (
        "termo_busca",
        "nicho",
        "fonte",
        "quadrante",
        "status",
        "total_encontrados",
        "total_sem_site",
        "total_novos",
        "total_fechados",
        "total_requisicoes",
        "criado_em",
    )
    # Filtrar por fonte é o que permite comparar cobertura Google x Foursquare
    # no mesmo quadrante.
    list_filter = ("status", "fonte", "segmento", "nicho", "cidade")
    readonly_fields = tuple(f.name for f in Varredura._meta.fields)

    def has_add_permission(self, request):
        # Varredura nasce da captação, nunca da mão.
        return False

    def get_urls(self):
        urls = super().get_urls()
        urls_nova_captacao = [
            path(
                "nova-captacao/",
                self.admin_site.admin_view(self.nova_captacao_view),
                name="leads_varredura_nova_captacao",
            )
        ]
        return urls_nova_captacao + urls

    def nova_captacao_view(self, request):
        """Adapta o serviço de Cidade para um fluxo Admin em duas etapas.

        Assinatura e nonce reduzem reenvio sequencial, mas a sessão não é uma
        reserva atômica: confirmações concorrentes ainda podem executar juntas.
        """
        if not request.user.is_superuser:
            raise PermissionDenied

        formulario = CaptacaoCidadeForm(request.POST or None)
        plano = None
        assinatura = ""

        if request.method == "POST" and "visualizar_plano" in request.POST:
            if formulario.is_valid():
                try:
                    plano = self._planejar_captacao(formulario)
                except CaptacaoCidadeError as exc:
                    formulario.add_error(None, str(exc))
                else:
                    assinatura = self._assinar_previa(request, formulario)

        elif request.method == "POST" and "confirmar_captacao" in request.POST:
            resposta = self._confirmar_captacao(request, formulario)
            if resposta is not None:
                return resposta

        contexto = {
            **self.admin_site.each_context(request),
            "title": "Nova Captação",
            "opts": self.model._meta,
            "formulario": formulario,
            "plano": plano,
            "assinatura_previa": assinatura,
            "segmento_rotulo": (
                Segmento(plano.segmento).label if plano is not None else ""
            ),
            "captacao_assinatura_max_age_minutos": (
                _CAPTACAO_ASSINATURA_MAX_AGE // 60
            ),
        }
        return TemplateResponse(
            request,
            "admin/leads/varredura/nova_captacao.html",
            contexto,
        )

    @staticmethod
    def _planejar_captacao(formulario: CaptacaoCidadeForm):
        dados = formulario.cleaned_data
        cidade, estado = dados["localidade"]
        return planejar_captacao_cidade(
            nicho_codigo=dados["nicho"].codigo,
            termo=dados["termo"],
            segmento=dados["segmento"],
            cidade=cidade,
            estado=estado,
            fonte=dados["fonte"],
            quadrante_rotulo=dados["quadrante"] or None,
        )

    def _assinar_previa(self, request, formulario: CaptacaoCidadeForm) -> str:
        nonce = secrets.token_urlsafe(24)
        nonces = list(request.session.get(_CAPTACAO_NONCES_SESSION_KEY, ()))
        nonces.append(nonce)
        request.session[_CAPTACAO_NONCES_SESSION_KEY] = nonces[
            -_CAPTACAO_NONCES_MAXIMOS:
        ]
        payload = {**formulario.dados_canonicos(), "nonce": nonce}
        return signing.dumps(payload, salt=_CAPTACAO_ASSINATURA_SALT, compress=True)

    def _confirmar_captacao(self, request, formulario: CaptacaoCidadeForm):
        assinatura = request.POST.get("assinatura_previa", "")
        try:
            payload = signing.loads(
                assinatura,
                salt=_CAPTACAO_ASSINATURA_SALT,
                max_age=_CAPTACAO_ASSINATURA_MAX_AGE,
            )
        except signing.SignatureExpired:
            formulario.add_error(
                None,
                "A prévia expirou. Visualize um novo plano antes de confirmar.",
            )
            return None
        except signing.BadSignature:
            formulario.add_error(
                None,
                "A prévia é inválida. Visualize um novo plano antes de confirmar.",
            )
            return None

        nonce = payload.get("nonce") if isinstance(payload, dict) else None
        if not self._consumir_nonce(request, nonce):
            formulario.add_error(
                None,
                "Esta prévia já foi usada ou não pertence a esta sessão. "
                "Visualize um novo plano.",
            )
            return None

        if not formulario.is_valid():
            formulario.add_error(
                None,
                "Os dados da captação não são mais válidos. Visualize um novo plano.",
            )
            return None

        dados_canonicos = formulario.dados_canonicos()
        chaves_esperadas = {*dados_canonicos, "nonce"}
        if set(payload) != chaves_esperadas or any(
            payload.get(chave) != valor
            for chave, valor in dados_canonicos.items()
        ):
            formulario.add_error(
                None,
                "Os dados foram alterados depois da prévia. Visualize um novo plano.",
            )
            return None

        try:
            plano = self._planejar_captacao(formulario)
        except CaptacaoCidadeError as exc:
            formulario.add_error(
                None,
                f"Não foi possível confirmar: {exc} Visualize um novo plano.",
            )
            return None

        try:
            resultado = executar_captacao_cidade(plano)
        except CaptacaoCidadeFalhaFinalizacaoError as exc:
            self._registrar_falha_finalizacao(exc)
            self._mensagem_resultado_seguro(request, exc.resultado)
            referencia = getattr(exc.varredura, "pk", None) or "sem ID"
            self.message_user(
                request,
                "Não foi possível persistir o encerramento da Varredura "
                f"{referencia}; o status ERRO não está confirmado. "
                "Os quadrantes seguintes não foram executados.",
                messages.ERROR,
            )
        except CaptacaoCidadeParcialError as exc:
            self._registrar_falha_parcial(exc)
            self._mensagem_resultado_seguro(request, exc.resultado)
            referencia = getattr(exc.varredura, "pk", None) or "sem ID"
            self.message_user(
                request,
                f"A Varredura {referencia} foi registrada com ERRO. "
                "Os quadrantes seguintes não foram executados.",
                messages.ERROR,
            )
        except CaptacaoCidadeError as exc:
            logger.warning(
                "evento=captacao_admin_preflight_recusado "
                "excecao_tipo=%s fase=preflight",
                type(exc).__name__,
            )
            self.message_user(
                request,
                "A captação não foi iniciada. Visualize um novo plano.",
                messages.ERROR,
            )
        else:
            self._mensagem_sucesso(request, resultado)

        return HttpResponseRedirect(reverse("admin:leads_varredura_changelist"))

    @staticmethod
    def _consumir_nonce(request, nonce: object) -> bool:
        """Consome o nonce antes da execução, sem prometer trava transacional."""
        if not isinstance(nonce, str):
            return False
        nonces = list(request.session.get(_CAPTACAO_NONCES_SESSION_KEY, ()))
        if nonce not in nonces:
            return False
        nonces.remove(nonce)
        request.session[_CAPTACAO_NONCES_SESSION_KEY] = nonces
        return True

    def _mensagem_sucesso(
        self,
        request,
        resultado: ResultadoCaptacaoCidade,
    ) -> None:
        if resultado.franquia_esgotada:
            self.message_user(
                request,
                f"Captação interrompida por franquia: "
                f"{len(resultado.varreduras)} Varredura(s) registrada(s), "
                f"{resultado.total_requisicoes} requisição(ões) e "
                f"{resultado.total_novos} prospect(s) novo(s).",
                messages.WARNING,
            )
            return

        self.message_user(
            request,
            f"Captação concluída: {len(resultado.varreduras)} Varredura(s) "
            f"registrada(s), {resultado.total_requisicoes} requisição(ões) e "
            f"{resultado.total_novos} prospect(s) novo(s).",
            messages.SUCCESS,
        )

    def _mensagem_resultado_seguro(
        self,
        request,
        resultado: ResultadoCaptacaoCidade,
    ) -> None:
        rotulos = ", ".join(
            varredura.quadrante.rotulo
            for varredura in resultado.varreduras
            if getattr(varredura, "quadrante", None) is not None
        )
        detalhe = f" Quadrantes registrados: {rotulos}." if rotulos else ""
        self.message_user(
            request,
            f"Resultado preservado: {len(resultado.varreduras)} Varredura(s), "
            f"{resultado.total_requisicoes} requisição(ões) e "
            f"{resultado.total_novos} prospect(s) novo(s).{detalhe}",
            messages.WARNING,
        )

    @staticmethod
    def _registrar_falha_parcial(exc: CaptacaoCidadeParcialError) -> None:
        ids_registrados = ",".join(
            str(varredura.pk) for varredura in exc.resultado.varreduras
        )
        logger.error(
            "evento=captacao_admin_falha_parcial causa_tipo=%s "
            "varredura_id=%s quadrante=%s varreduras_registradas_ids=%s "
            "varreduras_registradas_total=%d",
            type(exc.causa).__name__,
            getattr(exc.varredura, "pk", None),
            getattr(exc.quadrante, "rotulo", None),
            ids_registrados,
            len(exc.resultado.varreduras),
        )

    @staticmethod
    def _registrar_falha_finalizacao(
        exc: CaptacaoCidadeFalhaFinalizacaoError,
    ) -> None:
        ids_seguros = ",".join(
            str(varredura.pk) for varredura in exc.resultado.varreduras
        )
        logger.error(
            "evento=captacao_admin_falha_finalizacao "
            "causa_operacional_tipo=%s causa_finalizacao_tipo=%s "
            "varredura_id=%s quadrante=%s varreduras_seguras_ids=%s "
            "varreduras_seguras_total=%d",
            type(exc.causa_operacional).__name__,
            type(exc.causa_finalizacao).__name__,
            getattr(exc.varredura, "pk", None),
            getattr(exc.quadrante, "rotulo", None),
            ids_seguros,
            len(exc.resultado.varreduras),
        )


@admin.register(Quadrante)
class QuadranteAdmin(admin.ModelAdmin):
    list_display = ("rotulo", "cidade", "estado", "sul", "oeste", "norte", "leste")
    list_filter = ("cidade", "estado")


class InteracaoAdminForm(forms.ModelForm):
    segmento = forms.ChoiceField(
        choices=Segmento.choices,
        label="Segmento",
        required=True,
    )

    status_funil = forms.ChoiceField(
        choices=StatusFunil.choices,
        label="Situação do lead",
        required=True,
    )

    proximo_contato_em = forms.SplitDateTimeField(
        label="Próximo contato",
        required=False,
        widget=AdminSplitDateTime(),
    )

    ativo_no_funil = forms.BooleanField(
        label="Ativo no funil",
        required=False,
    )

    class Meta:
        model = Interacao
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if self.instance and self.instance.pk and self.instance.prospect_id:
            prospect = self.instance.prospect

            self.fields["segmento"].initial = prospect.segmento
            self.fields["status_funil"].initial = prospect.status_funil
            self.fields["proximo_contato_em"].initial = prospect.proximo_contato_em
            self.fields["ativo_no_funil"].initial = prospect.ativo_no_funil

    def save(self, commit=True):
        instance = super().save(commit=commit)

        if instance.prospect_id:
            prospect = instance.prospect

            prospect.segmento = self.cleaned_data["segmento"]
            prospect.status_funil = self.cleaned_data["status_funil"]
            prospect.proximo_contato_em = self.cleaned_data.get(
                "proximo_contato_em"
            )

            ativo = self.cleaned_data.get("ativo_no_funil", False)

            if prospect.status_funil in {
                StatusFunil.CONVERTIDO,
                StatusFunil.DESCARTADO,
            }:
                ativo = False

            prospect.ativo_no_funil = ativo
            prospect.revisado_manualmente = True
            prospect.atualizado_em = timezone.now()

            prospect.save(
                update_fields=[
                    "segmento",
                    "status_funil",
                    "proximo_contato_em",
                    "ativo_no_funil",
                    "revisado_manualmente",
                    "atualizado_em",
                ]
            )

        return instance


class RetornoFollowupFilter(admin.SimpleListFilter):
    title = "retorno"
    parameter_name = "retorno"

    def lookups(self, request, model_admin):
        return (
            ("pendente", "Retorno pendente"),
            ("agendado", "Retorno agendado"),
            ("sem_data", "Sem data de retorno"),
        )

    def queryset(self, request, queryset):
        agora = timezone.now()

        if self.value() == "pendente":
            return (
                queryset.filter(
                    prospect__ativo_no_funil=True,
                    prospect__proximo_contato_em__lte=agora,
                )
                .exclude(
                    prospect__status_funil__in={
                        StatusFunil.CONVERTIDO,
                        StatusFunil.DESCARTADO,
                        StatusFunil.NUMERO_INVALIDO,
                    }
                )
            )

        if self.value() == "agendado":
            return (
                queryset.filter(
                    prospect__ativo_no_funil=True,
                    prospect__proximo_contato_em__gt=agora,
                )
                .exclude(
                    prospect__status_funil__in={
                        StatusFunil.CONVERTIDO,
                        StatusFunil.DESCARTADO,
                        StatusFunil.NUMERO_INVALIDO,
                    }
                )
            )

        if self.value() == "sem_data":
            return queryset.filter(
                prospect__proximo_contato_em__isnull=True
            )

        return queryset


@admin.register(Interacao)
class InteracaoAdmin(admin.ModelAdmin):
    form = InteracaoAdminForm

    class Media:
        css = {
            "all": ("leads/admin_mobile.css",)
        }

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "<path:object_id>/segmento/",
                self.admin_site.admin_view(self.alterar_segmento_view),
                name="leads_interacao_segmento",
            ),
            path(
                "<path:object_id>/status/",
                self.admin_site.admin_view(self.alterar_status_view),
                name="leads_interacao_status",
            ),
        ]
        return custom_urls + urls

    list_display = (
        "prospect",
        "segmento_atual",
        "status_editavel",
        "retorno_followup",
        "canal",
        "criado_em",
        "enviar_modelo_lista",
        "enviar_proposta_lista",
        "abrir_whatsapp_lista",
    )
    list_filter = (
        RetornoFollowupFilter,
        "canal",
        "prospect__segmento",
        "prospect__status_funil",
    )
    search_fields = (
        "prospect__nome",
        "prospect__telefone",
        "anotacao",
    )
    autocomplete_fields = ("prospect",)

    readonly_fields = (
        "criado_em",
        "abrir_whatsapp",
    )

    fields = (
        "prospect",
        "segmento",
        "status_funil",
        "proximo_contato_em",
        "ativo_no_funil",
        "canal",
        "anotacao",
        "abrir_whatsapp",
        "criado_em",
    )

    def changelist_view(self, request, extra_context=None):
        agora = timezone.now()

        pendentes = (
            Prospect.objects.filter(
                ativo_no_funil=True,
                proximo_contato_em__lte=agora,
            )
            .exclude(
                status_funil__in={
                    StatusFunil.CONVERTIDO,
                    StatusFunil.DESCARTADO,
                    StatusFunil.NUMERO_INVALIDO,
                }
            )
            .count()
        )

        if pendentes and request.GET.get("retorno") != "pendente":
            url = (
                reverse("admin:leads_interacao_changelist")
                + "?retorno=pendente"
            )

            self.message_user(
                request,
                format_html(
                    'Você tem <strong>{}</strong> contato(s) com retorno '
                    'pendente. <a href="{}" style="font-weight:700;'
                    'text-decoration:underline;">Ver agora</a>',
                    pendentes,
                    url,
                ),
                messages.WARNING,
            )

        return super().changelist_view(request, extra_context=extra_context)

    @admin.display(
        description="Retorno",
        ordering="prospect__proximo_contato_em",
    )
    def retorno_followup(self, obj: Interacao):
        prospect = obj.prospect

        if prospect.status_funil in {
            StatusFunil.CONVERTIDO,
            StatusFunil.DESCARTADO,
            StatusFunil.NUMERO_INVALIDO,
        }:
            return "—"

        proximo = prospect.proximo_contato_em

        if not proximo:
            return "—"

        agora = timezone.localtime()
        retorno = timezone.localtime(proximo)

        hoje = agora.date()
        data_retorno = retorno.date()

        if retorno <= agora:
            dias = (hoje - data_retorno).days

            if dias <= 0:
                texto = f"HOJE {retorno:%H:%M}"
            elif dias == 1:
                texto = "ATRASADO 1 dia"
            else:
                texto = f"ATRASADO {dias} dias"

            return format_html(
                '<span style="background:#dc3545;color:white;'
                'padding:5px 8px;border-radius:6px;font-weight:700;'
                'white-space:nowrap;">{}</span>',
                texto,
            )

        if data_retorno == hoje:
            return format_html(
                '<span style="background:#f59f00;color:#111;'
                'padding:5px 8px;border-radius:6px;font-weight:700;'
                'white-space:nowrap;">HOJE {}</span>',
                retorno.strftime("%H:%M"),
            )

        if (data_retorno - hoje).days == 1:
            return format_html(
                '<span style="background:#ffc107;color:#111;'
                'padding:5px 8px;border-radius:6px;font-weight:600;'
                'white-space:nowrap;">AMANHÃ {}</span>',
                retorno.strftime("%H:%M"),
            )

        return retorno.strftime("%d/%m/%Y %H:%M")

    @admin.display(description="Segmento", ordering="prospect__segmento")
    def segmento_atual(self, obj: Interacao):
        action = reverse(
            "admin:leads_interacao_segmento",
            args=[obj.pk],
        )

        options = []

        for value, label in Segmento.choices:
            url = f"{action}?segmento={value}"
            selected = " selected" if obj.prospect.segmento == value else ""

            options.append(
                f'<option value="{url}"{selected}>{label}</option>'
            )

        return format_html(
            '<select aria-label="Segmento" onchange="window.location.href=this.value;" '
            'style="min-width:150px;padding:4px 6px;">{}</select>',
            format_html("".join(options)),
        )

    def alterar_segmento_view(self, request, object_id):
        obj = self.get_object(request, object_id)

        if obj is None:
            self.message_user(
                request,
                "Interação não encontrada.",
                messages.ERROR,
            )
            return HttpResponseRedirect(
                reverse("admin:leads_interacao_changelist")
            )

        novo_segmento = request.GET.get("segmento", "")

        segmentos_validos = {value for value, _ in Segmento.choices}

        if novo_segmento not in segmentos_validos:
            self.message_user(
                request,
                "Segmento inválido.",
                messages.ERROR,
            )
            return HttpResponseRedirect(
                reverse("admin:leads_interacao_changelist")
            )

        prospect = obj.prospect
        prospect.segmento = novo_segmento
        prospect.revisado_manualmente = True
        prospect.atualizado_em = timezone.now()
        prospect.save(
            update_fields=[
                "segmento",
                "revisado_manualmente",
                "atualizado_em",
            ]
        )

        self.message_user(
            request,
            f"Segmento de {prospect.nome} atualizado para "
            f"{prospect.get_segmento_display()}.",
            messages.SUCCESS,
        )

        return HttpResponseRedirect(
            reverse("admin:leads_interacao_changelist")
        )

    @admin.display(description="Status", ordering="prospect__status_funil")
    def status_editavel(self, obj: Interacao):
        action = reverse(
            "admin:leads_interacao_status",
            args=[obj.pk],
        )

        cores = {
            StatusFunil.VERIFICAR_SITE: "#6c757d",
            StatusFunil.NOVO: "#6f42c1",
            StatusFunil.INICIAR: "#495057",
            StatusFunil.EM_ANDAMENTO: "#0d6efd",
            StatusFunil.SEM_RESPOSTA: "#6f42c1",
            StatusFunil.NUMERO_INVALIDO: "#343a40",
            StatusFunil.REMARKETING: "#f59f00",
            StatusFunil.CONVERTIDO: "#198754",
            StatusFunil.DESCARTADO: "#dc3545",
        }

        cor = cores.get(obj.prospect.status_funil, "#6c757d")
        options = []

        for value, label in StatusFunil.choices:
            url = f"{action}?status={value}"
            selected = " selected" if obj.prospect.status_funil == value else ""

            options.append(
                f'<option value="{url}"{selected}>{label}</option>'
            )

        return format_html(
            '<select aria-label="Status" onchange="window.location.href=this.value;" '
            'style="min-width:155px;padding:5px 8px;'
            'background:{};color:white;font-weight:600;'
            'border:0;border-radius:6px;">{}</select>',
            cor,
            format_html("".join(options)),
        )

    def alterar_status_view(self, request, object_id):
        obj = self.get_object(request, object_id)

        if obj is None:
            self.message_user(
                request,
                "Interação não encontrada.",
                messages.ERROR,
            )
            return HttpResponseRedirect(
                reverse("admin:leads_interacao_changelist")
            )

        novo_status = request.GET.get("status", "")
        status_validos = {value for value, _ in StatusFunil.choices}

        if novo_status not in status_validos:
            self.message_user(
                request,
                "Status inválido.",
                messages.ERROR,
            )
            return HttpResponseRedirect(
                reverse("admin:leads_interacao_changelist")
            )

        prospect = obj.prospect
        prospect.status_funil = novo_status

        if novo_status in {
            StatusFunil.CONVERTIDO,
            StatusFunil.DESCARTADO,
            StatusFunil.NUMERO_INVALIDO,
        }:
            prospect.ativo_no_funil = False
            prospect.proximo_contato_em = None
        else:
            prospect.ativo_no_funil = True

        prospect.revisado_manualmente = True
        prospect.atualizado_em = timezone.now()

        prospect.save(
            update_fields=[
                "status_funil",
                "ativo_no_funil",
                "proximo_contato_em",
                "revisado_manualmente",
                "atualizado_em",
            ]
        )

        self.message_user(
            request,
            f"Status de {prospect.nome} atualizado para "
            f"{prospect.get_status_funil_display()}.",
            messages.SUCCESS,
        )

        return HttpResponseRedirect(
            reverse("admin:leads_interacao_changelist")
        )

    def _mensagem_whatsapp(self, prospect: Prospect) -> str:
        if prospect.status_funil == StatusFunil.SEM_RESPOSTA:
            if prospect.segmento == Segmento.BARBEARIA:
                return """Oi! Tudo bem?

Passando novamente porque te enviei uma mensagem esses dias sobre a presença digital da barbearia.

Não sei se conseguiu visualizar o modelo que mandei, então estou deixando novamente por aqui:
https://vitre-storefront.salvatiniguilherme.workers.dev/barber-demo

Se fizer sentido para vocês, posso te explicar rapidinho como funcionaria a personalização."""

            if prospect.segmento in {
                Segmento.SALAO,
                Segmento.ESTETICA,
                Segmento.NAIL,
                Segmento.LASH,
                Segmento.SOBRANCELHA,
            }:
                return """Oi! Tudo bem? 😊

Passando novamente porque te enviei uma mensagem esses dias sobre a presença digital de vocês.

Não sei se conseguiu visualizar o modelo que mandei, então estou deixando novamente por aqui:
https://d2604417.vitre-estetica-01-elara.pages.dev/

Se fizer sentido, posso te explicar rapidinho como funcionaria a personalização para vocês."""

            return """Oi! Tudo bem?

Passando novamente sobre a mensagem que te enviei esses dias a respeito da presença digital da empresa.

Não sei se conseguiu visualizar. Se fizer sentido para vocês, posso te explicar rapidinho como funciona a proposta."""


        if prospect.status_funil == StatusFunil.EM_ANDAMENTO:
            return """Oi! Tudo bem?

Passando para dar continuidade ao que conversamos sobre o site.

Se quiser, consigo te explicar os próximos passos e como funcionaria a versão personalizada para vocês."""


        if prospect.segmento == Segmento.BARBEARIA:
            return """Oi! Tudo bem? Sou o Guilherme, desenvolvedor aqui de Maringá.

Encontrei a barbearia de vocês pesquisando negócios da região e estou trabalhando com uma proposta simples para fortalecer a presença digital de empresas locais.

Hoje muita gente pesquisa no Google antes de escolher onde cortar o cabelo, fazer a barba ou conhecer uma barbearia. Ter um site próprio ajuda o negócio a ser encontrado, apresentar melhor os serviços, equipe, trabalhos e transmitir mais confiança para quem ainda não conhece vocês.

Desenvolvi este modelo para mostrar a ideia:
https://vitre-storefront.salvatiniguilherme.workers.dev/barber-demo

O plano de site estático começa em R$ 59,90/mês, com versão responsiva e estrutura preparada para indexação no Google, além de acesso direto ao WhatsApp/agendamento.

Se gostar do conceito, posso te mostrar como ficaria personalizado para a barbearia de vocês."""


        if prospect.segmento in {
            Segmento.SALAO,
            Segmento.ESTETICA,
            Segmento.NAIL,
            Segmento.LASH,
            Segmento.SOBRANCELHA,
        }:
            return """Oi! Tudo bem? Sou o Guilherme, desenvolvedor aqui de Maringá. 😊

Encontrei vocês pesquisando negócios da área de beleza da região e estou trabalhando pela VITRE com uma proposta de presença digital para empresas locais.

Hoje muita gente pesquisa no Google antes de escolher um salão, clínica ou profissional da área de beleza. Ter um site próprio ajuda o negócio a ser encontrado, apresentar melhor o ambiente, serviços, profissionais e resultados, além de transmitir mais confiança para quem ainda não conhece a marca.

Esse é um modelo demonstrativo:
https://d2604417.vitre-estetica-01-elara.pages.dev/

O plano de site estático começa em R$ 59,90/mês, com versão responsiva e estrutura preparada para indexação no Google, além de acesso direto ao WhatsApp/agendamento.

Se gostar da ideia, posso te mostrar como ficaria totalmente personalizado para vocês."""


        return """Oi! Tudo bem? Sou o Guilherme, desenvolvedor aqui de Maringá.

Encontrei vocês pesquisando negócios da região e estou trabalhando pela VITRE com uma proposta de presença digital para empresas locais.

Hoje muita gente pesquisa no Google antes de escolher uma empresa. Ter um site próprio ajuda o negócio a ser encontrado, apresentar melhor seus serviços e transmitir mais confiança para quem ainda não conhece a marca.

Os planos começam em R$ 59,90/mês, com site responsivo, estrutura preparada para indexação no Google e acesso direto ao WhatsApp.

Se tiver interesse, posso te mostrar alguns modelos."""


    def _mensagem_modelo_interessado(self, prospect: Prospect) -> str:
        if prospect.segmento == Segmento.BARBEARIA:
            return """Boa! É esse modelo aqui:

https://vitre-storefront.salvatiniguilherme.workers.dev/barber-demo

Ele é só uma demonstração da estrutura. A versão da barbearia seria personalizada com a identidade de vocês, serviços, equipe, trabalhos e contato/agendamento.

A ideia é ter um site próprio para complementar o Instagram e facilitar também para quem encontra vocês pesquisando no Google.

Dá uma olhada com calma e me fala o que achou."""

        if prospect.segmento in {
            Segmento.SALAO,
            Segmento.ESTETICA,
            Segmento.NAIL,
            Segmento.LASH,
            Segmento.SOBRANCELHA,
        }:
            return """Boa! Esse é o modelo que te falei:

https://d2604417.vitre-estetica-01-elara.pages.dev/

Ele é só uma demonstração da estrutura. A versão de vocês seria personalizada com identidade, serviços, imagens, profissionais e contatos.

A ideia é ter um site próprio para complementar o Instagram e facilitar também para quem encontra vocês pesquisando no Google.

Dá uma olhada com calma e me fala o que achou. 😊"""

        return """Boa! Separei um modelo para te mostrar melhor a ideia.

Ele é apenas uma demonstração da estrutura. A versão final seria personalizada com a identidade, serviços, imagens e contatos da empresa.

A ideia é ter um site próprio para complementar os canais que vocês já utilizam e apresentar melhor o negócio para quem encontra vocês online."""


    def _whatsapp_url_modelo(self, obj: Interacao):
        prospect = obj.prospect

        if not prospect.telefone or not prospect.telefone_e_celular:
            return None

        numero = "".join(filter(str.isdigit, prospect.telefone))
        mensagem = self._mensagem_modelo_interessado(prospect)

        return f"https://wa.me/{numero}?text={quote(mensagem)}"


    @admin.display(description="Modelo")
    def enviar_modelo_lista(self, obj: Interacao):
        if obj.prospect.status_funil != StatusFunil.EM_ANDAMENTO:
            return "—"

        url = self._whatsapp_url_modelo(obj)

        if not url:
            return "—"

        return format_html(
            '<a href="{}" target="_blank" '
            'style="background:#0d6efd;color:white;padding:5px 9px;'
            'border-radius:4px;text-decoration:none;white-space:nowrap;'
            'font-weight:600;">Enviar modelo</a>',
            url,
        )


    def _mensagem_proposta(self, prospect: Prospect) -> str:
        if prospect.segmento == Segmento.BARBEARIA:
            return """Que bom que curtiu!

Hoje estou trabalhando com duas opções:
site estático por R$ 59,90/mês e uma versão animada por R$ 69,90/mês.

Nos dois casos a ideia é personalizar tudo para a barbearia — identidade, serviços, equipe, trabalhos e contatos.

Se preferir, também tenho opção anual por R$ 500. O domínio fica separado."""

        if prospect.segmento in {
            Segmento.SALAO,
            Segmento.ESTETICA,
            Segmento.NAIL,
            Segmento.LASH,
            Segmento.SOBRANCELHA,
        }:
            return """Que bom que gostou! 😊

Hoje estou trabalhando com duas opções:
site estático por R$ 59,90/mês e uma versão animada por R$ 69,90/mês.

A versão de vocês seria personalizada com identidade, serviços, profissionais, imagens e contatos.

Também tenho opção anual por R$ 500. O domínio fica separado."""

        return """Que bom que gostou!

Hoje estou trabalhando com duas opções:
site estático por R$ 59,90/mês e uma versão animada por R$ 69,90/mês.

A versão final seria personalizada com a identidade, serviços, imagens e contatos da empresa.

Também tenho opção anual por R$ 500. O domínio fica separado."""


    def _whatsapp_url_proposta(self, obj: Interacao):
        prospect = obj.prospect

        if not prospect.telefone or not prospect.telefone_e_celular:
            return None

        numero = "".join(filter(str.isdigit, prospect.telefone))
        mensagem = self._mensagem_proposta(prospect)

        return f"https://wa.me/{numero}?text={quote(mensagem)}"


    @admin.display(description="Proposta")
    def enviar_proposta_lista(self, obj: Interacao):
        if obj.prospect.status_funil != StatusFunil.EM_ANDAMENTO:
            return "—"

        url = self._whatsapp_url_proposta(obj)

        if not url:
            return "—"

        return format_html(
            '<a href="{}" target="_blank" '
            'style="background:#6f42c1;color:white;padding:5px 9px;'
            'border-radius:4px;text-decoration:none;white-space:nowrap;'
            'font-weight:600;">Enviar proposta</a>',
            url,
        )


    def _whatsapp_url(self, obj: Interacao):
        prospect = obj.prospect

        if not prospect.telefone or not prospect.telefone_e_celular:
            return None

        numero = "".join(filter(str.isdigit, prospect.telefone))
        mensagem = self._mensagem_whatsapp(prospect)

        return f"https://wa.me/{numero}?text={quote(mensagem)}"

    @admin.display(description="WhatsApp")
    def abrir_whatsapp(self, obj: Interacao):
        if not obj or not obj.pk:
            return "Salve a interação primeiro."

        url = self._whatsapp_url(obj)

        if not url:
            return "Sem celular válido."

        return format_html(
            '<a href="{}" target="_blank" '
            'style="display:inline-block;background:#198754;color:white;'
            'padding:8px 14px;border-radius:4px;text-decoration:none;'
            'font-weight:600;">Abrir WhatsApp</a>',
            url,
        )

    @admin.display(description="WhatsApp")
    def abrir_whatsapp_lista(self, obj: Interacao):
        url = self._whatsapp_url(obj)

        if not url:
            return "—"

        return format_html(
            '<a href="{}" target="_blank" '
            'style="background:#198754;color:white;padding:5px 9px;'
            'border-radius:4px;text-decoration:none;white-space:nowrap;">'
            'Abrir WhatsApp</a>',
            url,
        )


@admin.register(Descarte)
class DescarteAdmin(admin.ModelAdmin):
    list_display = ("prospect", "motivo", "banido", "criado_em")
    list_filter = ("motivo", "banido")
    search_fields = ("prospect__nome",)


admin.site.site_header = "VITRE — Motor de Leads"
admin.site.site_title = "VITRE Leads"
admin.site.index_title = "Captação e funil"
