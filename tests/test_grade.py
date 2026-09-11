"""Testes da grade geográfica.

O que importa aqui é contiguidade exata: um gap entre células significa
estabelecimento que nunca é captado por busca nenhuma.
"""

from __future__ import annotations

import pytest

from leads.services.grade import BoundingBox, CelulaGrade, gerar_grade

# bbox aproximado de Maringá/PR.
MARINGA = BoundingBox(sul=-23.47, oeste=-52.03, norte=-23.35, leste=-51.88)


def test_quantidade_de_celulas_e_lado_ao_quadrado():
    assert len(gerar_grade(MARINGA, 4)) == 16
    assert len(gerar_grade(MARINGA, 1)) == 1


def test_rotulos_sequenciais_comecando_em_q1():
    celulas = gerar_grade(MARINGA, 3)
    assert [c.rotulo for c in celulas] == [f"Q{n}" for n in range(1, 10)]


def test_q1_no_canto_sudoeste_e_ultima_no_nordeste():
    celulas = gerar_grade(MARINGA, 2)
    assert celulas[0].sul == MARINGA.sul
    assert celulas[0].oeste == MARINGA.oeste
    assert celulas[-1].norte == MARINGA.norte
    assert celulas[-1].leste == MARINGA.leste


def test_celulas_cobrem_o_bbox_sem_gap_nem_overlap():
    """Borda compartilhada entre vizinhas tem de ser bit-a-bit idêntica."""
    lado = 4
    celulas = gerar_grade(MARINGA, lado)

    def em(i: int, j: int) -> CelulaGrade:
        return celulas[i * lado + j]

    for i in range(lado):
        for j in range(lado - 1):
            # Vizinhas na horizontal: leste de uma == oeste da seguinte.
            assert em(i, j).leste == em(i, j + 1).oeste

    for i in range(lado - 1):
        for j in range(lado):
            # Vizinhas na vertical: norte de uma == sul da de cima.
            assert em(i, j).norte == em(i + 1, j).sul


def test_uniao_das_celulas_e_o_bbox_inteiro():
    celulas = gerar_grade(MARINGA, 5)
    assert min(c.sul for c in celulas) == MARINGA.sul
    assert min(c.oeste for c in celulas) == MARINGA.oeste
    assert max(c.norte for c in celulas) == MARINGA.norte
    assert max(c.leste for c in celulas) == MARINGA.leste


def test_grade_e_deterministica():
    assert gerar_grade(MARINGA, 3) == gerar_grade(MARINGA, 3)


@pytest.mark.parametrize("lado", [0, -1])
def test_lado_invalido_levanta(lado):
    with pytest.raises(ValueError, match="lado deve ser"):
        gerar_grade(MARINGA, lado)


def test_bbox_invertido_levanta():
    invertido = BoundingBox(sul=-23.35, oeste=-52.03, norte=-23.47, leste=-51.88)
    with pytest.raises(ValueError, match="bbox inválido"):
        gerar_grade(invertido, 2)
