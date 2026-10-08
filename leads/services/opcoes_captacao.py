"""Filtros explícitos de descoberta; nenhum deles comprova atividade ou WhatsApp."""
from dataclasses import dataclass, fields
import re
import unicodedata

MODOS_BUSCA = (
    ("automatico", "Categoria do nicho/segmento (modo atual)"),
    ("texto", "Termo digitado, sem categoria automática"),
    ("categoria_texto", "Categoria + termo digitado (mais restrito)"),
)
PERFIS_RESULTADOS = (
    ("padrao", "Filtro atual do nicho"),
    ("financeiro", "Indícios de crédito / financiamento"),
    ("veiculos", "Indícios de loja / revenda de veículos"),
)

@dataclass(frozen=True, slots=True)
class OpcoesCaptacao:
    modo_busca: str = "automatico"
    perfil_resultados: str = "padrao"
    incluir_com_site: bool = False
    somente_celular: bool = False

    def validar(self, fonte):
        if self.modo_busca not in dict(MODOS_BUSCA):
            raise ValueError("Modo de busca inválido.")
        if self.perfil_resultados not in dict(PERFIS_RESULTADOS):
            raise ValueError("Perfil de resultados inválido.")
        if type(self.incluir_com_site) is not bool or type(self.somente_celular) is not bool:
            raise ValueError("As opções de site e celular devem ser booleanas.")
        if fonte != "foursquare" and self.modo_busca != "automatico":
            raise ValueError("Os modos de categoria/termo são exclusivos da Foursquare.")

    def argumentos(self):
        # Não muda os contratos de chamadas legadas com opções padrão.
        padrao = OpcoesCaptacao()
        return {f.name: getattr(self, f.name) for f in fields(self)
                if getattr(self, f.name) != getattr(padrao, f.name)}


def normalizar(valor):
    return "".join(c for c in unicodedata.normalize("NFKD", valor or "")
                   if not unicodedata.combining(c)).casefold()


def corresponde_ao_perfil(candidato, perfil):
    """Heurística conservadora por nome e categoria, com possíveis falsos negativos."""
    texto = normalizar(candidato.nome + " " + candidato.categoria)
    categoria = normalizar(candidato.categoria)
    if any(t in categoria for t in (
        "restaurant", "hamburger", "burger", "food", "barbershop", "beauty", "hair salon",
        "restaurante", "lanchonete", "salao de beleza", "padaria",
    )):
        return False
    if perfil == "financeiro":
        if any(t in texto for t in (
            "aula",
            "curso",
            "mba",
            "faculdade",
            "universidade",
            "escola",
            "treinamento",
            "contabil",
            "contabilidade",
            "auditoria",
            "secretaria",
        )):
            return False

        return bool(re.search(
            r"\b(correspond\w*|promotora\w*|cred\w*|financ\w*|emprest\w*|consorc\w*|loan\w*)",
            texto,
        ))
    if perfil == "veiculos":
        return bool(re.search(r"\b(veicul\w*|automove\w*|concessionar\w*|revenda\w*|motors|car dealer|used car)", texto))
    return True
