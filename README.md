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
2. **Captação** — uma varredura por quadrante, com Nicho comercial, Segmento
   técnico e termo explícito da atividade.
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

# 2. Modo legado: ver o custo antes de gastar (não chama a API)
uv run python manage.py captar --segmento SALAO --dry-run

# 3. Modo legado: captar com Nicho beleza e termo vindo do Segmento
uv run python manage.py captar --segmento SALAO
uv run python manage.py captar --segmento BARBEARIA
uv run python manage.py captar --segmento ESTETICA
uv run python manage.py captar --segmento NAIL
uv run python manage.py captar --segmento LASH

# 4. Modo novo: captar com Nicho e atividade explícitos
uv run python manage.py captar --nicho motoboys --termo "motoboy"

# Segmento é opcional no modo novo; quando omitido, usa OUTRO
uv run python manage.py captar --nicho motoboys --termo "motoboy" --segmento OUTRO

# 5. Trabalhar os leads
uv run python manage.py runserver     # → http://localhost:8000/admin
```

Segmentos: `SALAO`, `BARBEARIA`, `ESTETICA`, `NAIL`, `LASH`, `SOBRANCELHA`, `OUTRO`.

No modo novo, `--nicho` é o código exato de um Nicho que já deve estar
cadastrado e ativo. `--termo` contém apenas a atividade (por exemplo,
`motoboy`), sem cidade ou estado; o serviço acrescenta a localização uma única
vez e registra a consulta completa na Varredura. `--nicho` e `--termo` devem
ser usados juntos. `--dry-run` valida Nicho, fonte e grade e calcula a
franquia, mas não cria Varredura nem chama API.

Este incremento atende somente captação por **Cidade**, usando a grade já
gerada. No Django Admin, um superusuário pode abrir **Varreduras → Nova
Captação**, selecionar uma Cidade/UF entre as localidades que já possuem grade
cadastrada, preencher os demais dados do modo novo da CLI e visualizar o plano
antes de confirmar. Para disponibilizar outra cidade, primeiro é necessário
gerar ou cadastrar sua grade. A prévia não exige credencial, não cria Varredura
e não chama API; a confirmação revalida os dados, refaz o plano e chama
diretamente o mesmo serviço compartilhado pela CLI.

A prévia é assinada por 15 minutos e usa um nonce de uso único associado à
sessão. O nonce é consumido antes da execução, o botão é desabilitado no
primeiro clique e o resultado redireciona de volta à listagem de Varreduras.
Essas proteções reduzem reenvios acidentais no uso local, mas não oferecem
idempotência transacional nem impedem duas execuções concorrentes. A execução
é síncrona: pode demorar, e a aba deve permanecer aberta. Captação por Estado
ou Brasil, execução em background e progresso persistido continuam futuros.

## Custo

A unidade de cobrança controlada pelo sistema é a **requisição**: uma página
da fonte escolhida. No Google, cada página traz até 20 resultados e uma busca
usa no máximo 3 páginas; `PLACES_FRANQUIA_MENSAL` configura sua franquia. Na
Foursquare, cada página traz até 50 resultados, também com até 3 páginas por
busca, e `FOURSQUARE_FRANQUIA_MENSAL` mantém uma franquia independente. A
estimativa exibida pelo comando usa o máximo declarado pela fonte escolhida.

Controles atuais de consumo:

- O saldo da fonte é verificado novamente antes de iniciar cada quadrante.
- O contador respeita o fuso do reset da Google (Pacífico), não o nosso.
- Requisição consumida numa varredura que falhou **conta** na franquia, para
  que a trava não subestime o gasto real.

`--dry-run` mostra o pior caso antes de qualquer chamada.

Limitação preexistente: se o saldo restante for menor que o máximo de uma
busca, o quadrante já iniciado pode consumir mais de uma requisição. Execuções
concorrentes também exigem uma proteção específica. Essa dívida operacional
deve ser resolvida antes de captações amplas por Estado ou Brasil.

## Segmento vs CNAE

O CNAE 9602-5/01 junta salão de beleza e barbearia, e o `primaryType` do
Google é grosseiro demais. No modo legado, o label do Segmento fornece o termo
da atividade. No modo novo, o termo é explícito e o Segmento permanece como
contexto técnico, usando `OUTRO` por padrão. Assim não é preciso criar um
Segmento para cada Nicho nem inferir a atividade pelo nome fantasia. Correções
manuais no Admin ligam `revisado_manualmente` e nunca são desfeitas por uma
nova varredura.

## Nicho, segmento e localização

`Nicho` é a classificação comercial estável do prospect. O nicho inicial é
`beleza` / **Beleza**; novos nichos são cadastrados no Admin e precisam estar
ativos antes da captação. O modo legado com `--segmento` continua associando
implicitamente o Nicho `beleza`.

`Segmento` continua sendo o contexto técnico já existente: ele participa da
busca atual e escolhe a copy manual de WhatsApp. Não é substituído por nicho;
as escolhas e mensagens atuais permanecem intactas.

`Varredura.cidade` e `Varredura.estado` representam o **alvo da busca**. Já a
localização em `Prospect` representa somente o que a fonte declarou para o
estabelecimento, ou uma correção manual posterior. Nunca há fallback do alvo
da busca para o prospect.

`Prospect.origem_localizacao` informa se esse conjunto factual é
`DESCONHECIDA`, veio da `FONTE` ou foi corrigido `MANUAL`. Os 400 prospects
históricos não foram limpos nesta etapa: sua revisão é uma operação futura e
explícita.

## Testes

```bash
uv run pytest
```

Estado atual após o Incremento A1 do Admin: **247 testes passando**, com 2 warnings preexistentes.

Estado atual após o Incremento A2.1 do Admin: **252 testes passando**, com 2 warnings preexistentes.

## Stack

Django 5.2 · SQLite · httpx · Google Places API (New) · Geocoding API.
Roda local, sem servidor e sem container.


### Pesquisa mais precisa na Foursquare

Em Nova Captação, escolha como pesquisar: categoria automática (comportamento
anterior), termo digitado, ou categoria + termo. A prévia mostra a busca efetiva.
Nos dois modos explícitos, a Foursquare recebe somente o termo da atividade;
a localização continua restrita pelos cantos dos quadrantes. No modo automático,
uma categoria mapeada ignora o termo: trocar apenas o texto pode repetir os resultados.

Perfis opcionais filtram indícios de crédito/financiamento ou lojas/revendas de
veículos no nome e na categoria devolvidos pela fonte. Categorias de alimentação
e beleza são vetadas nesses perfis. Esses indícios não comprovam atividade
de correspondente nem financiamento automotivo e podem excluir negócios relevantes.
O perfil de revendas substitui o filtro financeiro do nicho, permitindo descobrir
lojas em uma campanha de correspondentes sem exigir palavras de crédito no nome.

“Incluir empresas com site” evita descartar uma empresa por já ter site; o site
continua registrado e não a qualifica automaticamente para a campanha original de
negócios sem site. “Guardar somente resultados com celular válido” é opcional:
o formato não comprova WhatsApp e números ausentes não são recuperados.
Os filtros locais não reduzem as requisições já consumidas pela API.

Exemplo de prévia para lojas de veículos (sem consumo de API):

```bash
uv run python manage.py captar --nicho correspondentes_bancarios \
  --termo "automóveis" --segmento OUTRO --cidade Joinville --estado SC \
  --fonte foursquare --modo-busca texto --perfil-resultados veiculos \
  --incluir-com-site --dry-run
```

Para crédito, use o perfil `financeiro` e termos separados como `financiamento`,
`promotora` ou `crédito`. Categoria + termo pode restringir a lista excessivamente;
compare primeiro uma rodada pequena e confira a qualidade antes de ampliar.

Os novos filtros fazem parte do payload assinado; alterar qualquer um depois da
prévia exige um plano novo. Confirmação, CSRF, nonce, franquia e replanejamento
continuam ativos. O botão WhatsApp mantém a abertura manual, com mensagem editável
e celular válido; não envia automaticamente nem confirma cadastro no WhatsApp.

A abrangência continua por cidade com grade cadastrada. Santa Catarina inteira
exige grades e execuções em várias cidades: Joinville não representa todo o estado.
Nenhum histórico é apagado ou reclassificado por esta atualização. As opções da
rodada ficam na prévia assinada, não em novas colunas de Varredura; o histórico
continua guardando termo completo, fonte, cidade e quadrante.
