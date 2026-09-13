"""Formulários de entrada para operações administrativas de leads."""

from __future__ import annotations

import json

from django import forms
from django.conf import settings

from leads.models import Nicho, Quadrante, Segmento
from leads.sources import FONTE_PADRAO, FONTES


class CaptacaoCidadeForm(forms.Form):
    """Entrada de uma captação por Cidade; regras de domínio ficam no serviço."""

    nicho = forms.ModelChoiceField(
        queryset=Nicho.objects.none(),
        label="Nicho",
        empty_label="Selecione um Nicho ativo",
        help_text="O Nicho precisa estar cadastrado e ativo.",
    )
    termo = forms.CharField(
        label="Termo da atividade",
        max_length=200,
        strip=True,
        help_text='Informe somente a atividade, por exemplo "motoboy".',
    )
    segmento = forms.ChoiceField(
        label="Segmento técnico",
        choices=Segmento.choices,
        initial=Segmento.OUTRO,
        help_text="Use Outro quando não houver um Segmento técnico específico.",
    )
    localidade = forms.ChoiceField(
        label="Cidade/UF",
        choices=(),
        required=False,
        help_text=(
            "Somente localidades com grade cadastrada estão disponíveis. "
            "Para adicionar outra cidade, gere ou cadastre sua grade primeiro."
        ),
    )
    fonte = forms.ChoiceField(
        label="Fonte",
        choices=(),
        help_text="A franquia e a estimativa são independentes por fonte.",
    )
    quadrante = forms.CharField(
        label="Quadrante",
        max_length=10,
        required=False,
        strip=True,
        help_text="Deixe vazio para pesquisar todos os quadrantes da cidade.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["nicho"].queryset = Nicho.objects.filter(ativo=True)
        self.fields["fonte"].choices = [
            (nome, nome.replace("_", " ").title()) for nome in FONTES
        ]
        self.fields["fonte"].initial = FONTE_PADRAO
        localidades = Quadrante.objects.order_by("estado", "cidade").values_list(
            "cidade",
            "estado",
        ).distinct()
        self._localidades = {
            self._codificar_localidade(cidade, estado): (cidade, estado)
            for cidade, estado in localidades
        }
        if self._localidades:
            rotulo_vazio = "Selecione uma Cidade/UF"
        else:
            rotulo_vazio = "Nenhuma Cidade/UF com grade cadastrada"
        self.fields["localidade"].choices = [
            ("", rotulo_vazio),
            *(
                (valor, f"{cidade}/{estado}")
                for valor, (cidade, estado) in self._localidades.items()
            ),
        ]
        localidade_padrao = self._codificar_localidade(
            settings.CIDADE_PADRAO,
            settings.ESTADO_PADRAO,
        )
        if localidade_padrao in self._localidades:
            self.fields["localidade"].initial = localidade_padrao

    @staticmethod
    def _codificar_localidade(cidade: str, estado: str) -> str:
        return json.dumps((cidade, estado), ensure_ascii=False, separators=(",", ":"))

    def clean_localidade(self) -> tuple[str, str]:
        valor = self.cleaned_data["localidade"]
        if not valor:
            if not self._localidades:
                raise forms.ValidationError(
                    "Nenhuma Cidade/UF possui grade cadastrada. "
                    "Gere ou cadastre a grade antes de captar."
                )
            raise forms.ValidationError("Selecione uma Cidade/UF com grade cadastrada.")
        return self._localidades[valor]

    def dados_canonicos(self) -> dict[str, str]:
        """Representação textual mínima assinada entre prévia e confirmação."""
        nicho = self.cleaned_data["nicho"]
        cidade, estado = self.cleaned_data["localidade"]
        return {
            "nicho": nicho.codigo,
            "termo": self.cleaned_data["termo"],
            "segmento": self.cleaned_data["segmento"],
            "cidade": cidade,
            "estado": estado,
            "fonte": self.cleaned_data["fonte"],
            "quadrante": self.cleaned_data["quadrante"],
        }
