"""Admin — a tela de trabalho diária: curadoria, funil e follow-up.

O Admin do Django faz aqui o papel do painel: filtro por segmento e status,
edição em lote e busca. É de propósito denso e sem enfeite — a ferramenta é
usada várias vezes por dia e o que importa é velocidade.
"""

from __future__ import annotations

from django.contrib import admin, messages
from django.db.models import BooleanField, Case, Q, QuerySet, Value, When
from django.utils import timezone

from leads.models import (
    Descarte,
    Interacao,
    Prospect,
    ProspectVerificacao,
    Quadrante,
    Socio,
    StatusFunil,
    Varredura,
)


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


@admin.register(Prospect)
class ProspectAdmin(admin.ModelAdmin):
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
class ProspectVerificacaoAdmin(admin.ModelAdmin):
    """Fila de verificação manual de site.

    Tela separada de propósito: o `ProspectAdmin` continua servindo o fluxo
    do Google exatamente como antes, e esta fila tem regra própria.

    Ordenação por TELEFONE primeiro, não por avaliações. A Foursquare não dá
    nota nem contagem de avaliação sem campo pago (`FOURSQUARE_CAMPOS_PRO`
    segue False), então `-total_avaliacoes` ordenaria 400 nulos — ordem
    aleatória na prática. Telefone preenchido (158 dos 400) é o único sinal
    disponível de que vale gastar revisão manual: sem telefone não há como
    abordar, mesmo que o lead se confirme.
    """

    list_display = (
        "nome",
        "tem_telefone",
        "telefone",
        "tag_verificacao",
        "segmento",
        "endereco",
        "website_url",
        "site_evidencia",
    )
    list_filter = ("origem", "segmento", "cidade")
    search_fields = ("nome", "telefone", "endereco")
    readonly_fields = ("origem", "origem_id", "criado_em", "atualizado_em", "varredura")
    actions = ("confirmar_sem_site", "descartar_tem_site")

    @admin.display(description="Verificação")
    def tag_verificacao(self, obj: Prospect) -> str:
        return obj.tag_verificacao

    @admin.display(description="Tel?", boolean=True, ordering="_tem_telefone")
    def tem_telefone(self, obj: Prospect) -> bool:
        return bool(obj.telefone)

    def get_queryset(self, request) -> QuerySet[ProspectVerificacao]:
        # Sem `super()` de propósito: `ModelAdmin.get_queryset` já aplica
        # `order_by` antes de qualquer anotação existir, e ordenar por
        # `_tem_telefone` ali estoura FieldError. Aqui a ordem é anotar →
        # ordenar.
        qs = self.model._default_manager.get_queryset().filter(
            status_funil=StatusFunil.VERIFICAR_SITE
        )
        qs = qs.annotate(
            _tem_telefone=Case(
                When(~Q(telefone=""), then=Value(True)),
                default=Value(False),
                output_field=BooleanField(),
            )
        )
        return qs.order_by(*self.get_ordering(request))

    def get_ordering(self, request) -> tuple[str, ...]:
        # Em `get_ordering` e não no atributo `ordering` porque o system check
        # admin.E033 valida `ordering` contra campos do modelo, e
        # `_tem_telefone` é anotação.
        return ("-_tem_telefone", "nome")

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
    list_display = (
        "termo_busca",
        "fonte",
        "quadrante",
        "status",
        "total_encontrados",
        "total_sem_site",
        "total_novos",
        "total_requisicoes",
        "criado_em",
    )
    # Filtrar por fonte é o que permite comparar cobertura Google x Foursquare
    # no mesmo quadrante.
    list_filter = ("status", "fonte", "segmento", "cidade")
    readonly_fields = tuple(f.name for f in Varredura._meta.fields)

    def has_add_permission(self, request):
        # Varredura nasce da captação, nunca da mão.
        return False


@admin.register(Quadrante)
class QuadranteAdmin(admin.ModelAdmin):
    list_display = ("rotulo", "cidade", "estado", "sul", "oeste", "norte", "leste")
    list_filter = ("cidade", "estado")


@admin.register(Interacao)
class InteracaoAdmin(admin.ModelAdmin):
    list_display = ("prospect", "canal", "criado_em")
    list_filter = ("canal",)
    search_fields = ("prospect__nome", "anotacao")


@admin.register(Descarte)
class DescarteAdmin(admin.ModelAdmin):
    list_display = ("prospect", "motivo", "banido", "criado_em")
    list_filter = ("motivo", "banido")
    search_fields = ("prospect__nome",)


admin.site.site_header = "VITRE — Motor de Leads"
admin.site.site_title = "VITRE Leads"
admin.site.index_title = "Captação e funil"
