# vitre-leads-engine

Motor de captação de leads da VITRE Digital: encontra salões de beleza,
clínicas de estética, nail e lash designers em Maringá/PR **que ainda não têm
site**, e organiza esses prospects num funil de venda manual.

O sistema **nunca envia mensagem**. Não há bot de WhatsApp, não há disparo
automático, não há contato automatizado em canal nenhum. Ele capta, filtra,
deduplica, prioriza e lembra. A abordagem, a negociação e o fechamento são
sempre seus.

## Como funciona

1. **Grade** — a cidade é geocodificada e subdividida em quadrantes. A Places
   API devolve no máximo 60 resultados por busca; buscar "salão de beleza em
   Maringá" satura nesse teto e mostra só os mais bem ranqueados — que são
   justamente os que já têm site. Buscando quadrante a quadrante, cada célula
   tem seu próprio teto e a cidade é varrida inteira.
2. **Captação** — uma varredura por quadrante, com o termo do segmento.
3. **Filtro de qualificação** — para cada estabelecimento, decide se ele tem
   site próprio de verdade. O `websiteUri` do Google mente: vem preenchido com
   Instagram, Linktree, página de agendamento, domínio morto e página
   estacionada. Quem **não** tem site é o alvo.
4. **Curadoria** — os captados entram como `NOVO` e você aprova em lote no
   Admin antes de virarem funil.
5. **Funil e follow-up** — interações registradas à mão, com data de próximo
   contato.

## Instalação

```bash
cd ~/projetos/vitre-leads-engine
uv sync
cp .env.example .env      # preencher GOOGLE_PLACES_API_KEY
uv run python manage.py migrate
uv run python manage.py createsuperuser
```

### Chave da Google

No console do Google Cloud: criar projeto, habilitar **Places API (New)** e
**Geocoding API**, gerar chave e restringi-la a essas duas APIs. Exige cartão
cadastrado, mas a franquia gratuita cobre folgado o uso abaixo — e o sistema
tem trava própria de custo.

## Uso

```bash
# 1. Uma vez por cidade: gera a grade de busca
uv run python manage.py gerar_grade --cidade Maringá --estado PR --lado 4

# 2. Ver o custo antes de gastar (não chama a API)
uv run python manage.py captar --segmento SALAO --dry-run

# 3. Captar
uv run python manage.py captar --segmento SALAO
uv run python manage.py captar --segmento ESTETICA
uv run python manage.py captar --segmento NAIL
uv run python manage.py captar --segmento LASH

# 4. Trabalhar os leads
uv run python manage.py runserver     # → http://localhost:8000/admin
```

Segmentos: `SALAO`, `ESTETICA`, `NAIL`, `LASH`, `SOBRANCELHA`, `OUTRO`.

## Custo

A unidade de cobrança é a **requisição** (1 requisição = 1 página = até 20
resultados). Uma grade 4x4 custa no máximo 48 requisições por segmento — a
cidade inteira, para um segmento. Os cinco segmentos cabem em ~240
requisições, contra a franquia padrão de 1000/mês configurada no `.env`.

Três travas contra fatura surpresa:

- `PLACES_FRANQUIA_MENSAL` no `.env` — teto absoluto. Atingido, a captação
  para.
- O contador respeita o fuso do reset da Google (Pacífico), não o nosso.
- Requisição consumida numa varredura que falhou **conta** na franquia, para
  a trava nunca subestimar o gasto real.

`--dry-run` mostra o pior caso antes de qualquer chamada.

## Segmento vs CNAE

O CNAE 9602-5/01 junta salão de beleza e barbearia, e o `primaryType` do
Google é grosseiro demais. Aqui o segmento vem do **termo de busca** que
capturou o prospect — você roda uma varredura por termo e não precisa de
heurística sobre o nome fantasia. Correções manuais no Admin ligam
`revisado_manualmente` e nunca são desfeitas por uma nova varredura.

## Testes

```bash
uv run pytest
```

## Stack

Django 5.2 · SQLite · httpx · Google Places API (New) · Geocoding API.
Roda local, sem servidor e sem container.
