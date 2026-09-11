"""Geocodifica uma cidade e grava a grade de quadrantes.

    uv run python manage.py gerar_grade --cidade Maringá --estado PR --lado 4

Roda uma vez por cidade. Não consome a franquia da Places API (Geocoding é
SKU separado).
"""

from __future__ import annotations

import asyncio

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from leads.models import Quadrante
from leads.services.grade import gerar_grade
from leads.sources.geocoding import GeocodingError, geocodar_cidade


class Command(BaseCommand):
    help = "Gera a grade de quadrantes de uma cidade a partir do bbox geocodado."

    def add_arguments(self, parser):
        parser.add_argument("--cidade", default=settings.CIDADE_PADRAO)
        parser.add_argument("--estado", default=settings.ESTADO_PADRAO)
        parser.add_argument(
            "--lado",
            type=int,
            default=4,
            help="Grade lado x lado. 4 = 16 quadrantes (padrão, cobre Maringá).",
        )

    def handle(self, *args, **opts):
        cidade, estado, lado = opts["cidade"], opts["estado"], opts["lado"]

        if not settings.GOOGLE_PLACES_API_KEY:
            raise CommandError("GOOGLE_PLACES_API_KEY ausente — preencha o .env.")

        try:
            bbox = asyncio.run(
                geocodar_cidade(cidade, estado, settings.GOOGLE_PLACES_API_KEY)
            )
        except GeocodingError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            f"bbox {cidade}/{estado}: "
            f"S{bbox.sul:.4f} O{bbox.oeste:.4f} N{bbox.norte:.4f} L{bbox.leste:.4f}"
        )

        criados = 0
        for celula in gerar_grade(bbox, lado):
            _, novo = Quadrante.objects.update_or_create(
                cidade=cidade,
                estado=estado,
                rotulo=celula.rotulo,
                defaults={
                    "sul": celula.sul,
                    "oeste": celula.oeste,
                    "norte": celula.norte,
                    "leste": celula.leste,
                },
            )
            criados += int(novo)

        total = lado * lado
        self.stdout.write(
            self.style.SUCCESS(
                f"Grade {lado}x{lado} pronta: {total} quadrantes "
                f"({criados} novos) para {cidade}/{estado}."
            )
        )
