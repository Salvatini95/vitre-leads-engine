"""Seleção cumulativa de cidades factuais no Admin de Prospects."""
import json
from django.contrib import admin
from django.contrib.admin.options import IncorrectLookupParameters


class ProspectCidadesFilter(admin.SimpleListFilter):
    title = "cidades (selecione várias)"
    parameter_name = "cidades"

    def __init__(self, request, params, model, model_admin):
        self.legado = request.GET.get("cidade")
        super().__init__(request, params, model, model_admin)

    def lookups(self, request, model_admin):
        return [(cidade, cidade) for cidade in
                model_admin.get_queryset(request).exclude(cidade__isnull=True)
                .exclude(cidade="").order_by("cidade")
                .values_list("cidade", flat=True).distinct()]

    def selecionadas(self):
        if not self.value():
            return [self.legado] if self.legado else []
        try:
            valores = json.loads(self.value())
        except (ValueError, TypeError):
            raise IncorrectLookupParameters("Seleção de cidades inválida")
        if not isinstance(valores, list) or len(valores) > 500 or any(
            not isinstance(v, str) for v in valores
        ):
            raise IncorrectLookupParameters("Seleção de cidades inválida")
        return sorted(set(valores))

    def choices(self, changelist):
        selecionadas = self.selecionadas()
        yield {"selected": not selecionadas, "display": "Todas as cidades",
               "query_string": changelist.get_query_string(
                   remove=[self.parameter_name, "cidade", "cidade__exact", "p"])}
        for valor, nome in self.lookup_choices:
            ativo = valor in selecionadas
            novos = [v for v in selecionadas if v != valor] if ativo else selecionadas + [valor]
            yield {"selected": ativo, "display": ("☑ " if ativo else "☐ ") + nome,
                   "query_string": changelist.get_query_string(
                       {self.parameter_name: json.dumps(sorted(novos), ensure_ascii=False)} if novos else {},
                       remove=["cidade", "cidade__exact", "p"] + ([] if novos else [self.parameter_name]))}

    def queryset(self, request, queryset):
        cidades = self.selecionadas()
        return queryset.filter(cidade__in=cidades) if cidades else queryset
