"""Grade geográfica — subdivide uma cidade em quadrantes de busca.

Por que isso existe: a Places API devolve no máximo 60 resultados por busca
(3 páginas de 20). Uma busca "salão de beleza em Maringá" satura nesse teto e
você nunca enxerga o resto da cidade — fica com uma amostra enviesada pelos
mais bem ranqueados, que são justamente os que já têm site.

Subdividindo a cidade em `lado x lado` células e buscando dentro de cada uma
(via `locationRestriction.rectangle`), cada célula tem seu próprio teto de 60.
Uma grade 4x4 sobre Maringá dá 16 x 60 = até 960 resultados por termo, o que
cobre a cidade inteira com folga.

Interpolação linear pura sobre o bbox: sem raio da Terra, sem conversão
graus→metros, sem diagonal. Determinístico — mesma entrada, mesma grade.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BoundingBox:
    """Retângulo geográfico de uma cidade, como devolvido pelo geocoding."""

    sul: float
    oeste: float
    norte: float
    leste: float


@dataclass(frozen=True, slots=True)
class CelulaGrade:
    """Uma célula calculada, pronta para virar `Quadrante`."""

    rotulo: str
    sul: float
    oeste: float
    norte: float
    leste: float


def gerar_grade(bbox: BoundingBox, lado: int) -> list[CelulaGrade]:
    """Subdivide `bbox` em `lado x lado` células iguais.

    Convenção de rótulo e ordem (estável — o mapa da tela de captação vai
    desenhar na mesma ordem):

    - `Q1`..`Q{lado*lado}`, varrendo em linha: percorre todas as colunas de
      uma linha (oeste → leste), depois sobe para a linha seguinte (sul →
      norte).
    - `Q1` fica no canto SUDOESTE; a última célula, no canto NORDESTE.

    Com `lado=2`::

        Q3 Q4    (linha de cima, norte)
        Q1 Q2    (linha de baixo, sul)

    As bordas compartilhadas são calculadas pelo MESMO índice — o `norte` da
    linha `i` e o `sul` da linha `i+1` saem ambos de
    `sul + (norte-sul) * (i+1) / lado`. Isso garante contiguidade exata, sem
    o gap ou overlap que uma soma acumulada de passo introduziria por erro de
    ponto flutuante. Buraco na grade = estabelecimento nunca captado.

    Raises:
        ValueError: se `lado` < 1 ou se o bbox estiver invertido.
    """
    if lado < 1:
        raise ValueError(f"lado deve ser >= 1, recebido {lado}")
    if bbox.norte <= bbox.sul or bbox.leste <= bbox.oeste:
        raise ValueError("bbox inválido: norte deve ser > sul e leste > oeste")

    celulas: list[CelulaGrade] = []
    contador = 1

    for i in range(lado):
        cel_sul = bbox.sul + (bbox.norte - bbox.sul) * i / lado
        cel_norte = bbox.sul + (bbox.norte - bbox.sul) * (i + 1) / lado

        for j in range(lado):
            cel_oeste = bbox.oeste + (bbox.leste - bbox.oeste) * j / lado
            cel_leste = bbox.oeste + (bbox.leste - bbox.oeste) * (j + 1) / lado

            celulas.append(
                CelulaGrade(
                    rotulo=f"Q{contador}",
                    sul=cel_sul,
                    oeste=cel_oeste,
                    norte=cel_norte,
                    leste=cel_leste,
                )
            )
            contador += 1

    return celulas
