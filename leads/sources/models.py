"""Tipos compartilhados entre as fontes de captação.

`ProspectCandidate` é o formato neutro que toda fonte devolve — Places hoje,
dataset CNPJ depois. A camada de persistência só conhece este tipo, então
acrescentar fonte não mexe no resto do sistema.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProspectCandidate:
    """Um estabelecimento como veio da fonte, antes de qualquer filtro.

    `origem_id` é o identificador nativo da fonte (place_id ou CNPJ) e é o que
    garante a unicidade e o dedup na recaptura.

    `rating` é sempre na escala 0–5, a do Google. Fonte que pontue em outra
    escala normaliza antes — o Admin ordena a curadoria por nota e misturar
    escalas desordena a fila sem dar erro.
    """

    origem: str
    origem_id: str
    nome: str
    endereco: str = ""
    telefone: str = ""
    website_url: str = ""
    rating: float | None = None
    total_avaliacoes: int | None = None
    # Rótulo de categoria da própria fonte (a Foursquare devolve; o Google
    # não, no field mask atual). Ainda não persistido em `Prospect` — serve
    # de material de auditoria da curadoria.
    categoria: str = ""
    ativo: bool = True


@dataclass(frozen=True, slots=True)
class ResultadoBusca:
    """Retorno de uma busca paginada.

    `total_requisicoes` é a unidade real de cobrança: 1 requisição HTTP = 1
    página = até 20 resultados. É gravado na varredura mesmo quando a busca
    falha no meio, porque requisição já consumida conta na franquia de
    qualquer jeito.
    """

    candidatos: list[ProspectCandidate]
    total_requisicoes: int


class BuscaParcialError(Exception):
    """Falha numa busca paginada, carregando o progresso já consumido.

    O chamador precisa gravar `total_requisicoes` na varredura antes de
    propagar — senão a franquia fica com contagem menor que o gasto real e a
    trava de custo deixa de proteger.
    """

    def __init__(
        self,
        *,
        total_requisicoes: int,
        candidatos: list[ProspectCandidate],
    ) -> None:
        self.total_requisicoes = total_requisicoes
        self.candidatos = candidatos
        super().__init__(
            f"Busca falhou após {total_requisicoes} requisição(ões) consumida(s)"
        )
