# Estado da sessão — vitre-leads-engine

> Documento de continuidade. Quem assumir a próxima sessão (Opus, Son Coder ou
> Codex) lê este arquivo primeiro.

**Última atualização:** 2026-09-10
**Fase:** 1 — captação e qualificação (concluída) + fonte de contingência

---

## O que está pronto e verificado

Bootstrap completo, 158 testes passando (`uv run pytest`).

| Área | Arquivo | Estado |
|---|---|---|
| Modelo de dados | `leads/models.py` | entidades de captação, CRM e `Nicho` + proxy `ProspectVerificacao` |
| Grade geográfica | `leads/services/grade.py` | testado, contiguidade garantida |
| Cliente Places | `leads/sources/google_places.py` | paginação, retry, field mask travado |
| Contrato de fonte | `leads/sources/base.py` + `__init__.py` | interface + registro das fontes |
| Cliente Foursquare | `leads/sources/foursquare.py` | contingência, busca por categoria |
| Geocoding | `leads/sources/geocoding.py` | bbox de cidade |
| Filtro sem-site | `leads/filters/site_validator.py` + `blacklist.yml` | 3 camadas, com evidência |
| Orquestração | `leads/services/captacao.py` | franquia, dedup, banimento |
| Comandos | `manage.py gerar_grade` / `captar` | `--dry-run` funcional |
| Painel | `leads/admin.py` | curadoria em lote, funil, follow-up, fila de verificação |

### Nicho e Segmento — estrutura aplicada

A migration `0011_nicho_prospect_nicho_varredura_nicho` foi aplicada ao banco
local. Ela cria o nicho comercial inicial
`beleza` / **Beleza**, associa apenas o lote histórico cuja varredura de origem
comprova a campanha de beleza e torna `Prospect.nicho` obrigatório.

- **Nicho** é a classificação comercial estável do prospect.
- **Segmento** permanece sendo o contexto técnico de busca e a seleção da copy
  manual de WhatsApp; não foi renomeado nem teve escolhas alteradas.
- Localização por cidade, estado ou Brasil como estratégia de captação é uma
  etapa futura independente e não está incluída nesta migration.

### Localização factual do Prospect — Etapa 2

- `Varredura.cidade` e `Varredura.estado` continuam sendo exclusivamente o
  alvo da busca; não podem ser usados como fallback factual do prospect.
- `Prospect` guarda a localização declarada pela fonte ou corrigida no Admin,
  com `origem_localizacao` em `DESCONHECIDA`, `FONTE` ou `MANUAL`.
- A migration estrutural `0012_localizacao_factual_do_prospect` foi aplicada
  com sucesso. Ela não contém backfill, limpeza ou reinterpretação: os 400
  históricos permanecem inalterados. A revisão desses dados exigirá etapa
  humana específica, pois o esquema anterior não registra autoria por campo
  geográfico.

**Verificação atual da Etapa 2 (após aplicar a migration 0012):**

- Migrations `0001` até `0012` aplicadas no banco local.
- `uv run python manage.py check` → sem problemas.
- `uv run pytest` → **176 testes passando**.
- `uv run python manage.py makemigrations --check` → `No changes detected`.
- `git diff --check` → passou.

**Auditoria pós-migration:**

| Métrica | Valor |
|---|---|
| Prospects preservados | **400** |
| Com endereço preenchido | **399** |
| Ainda com cidade `Maringá` e estado `PR` | **400** |
| País, CEP, latitude e longitude `NULL` | **400** |
| Pares incompletos de coordenadas | **0** |
| `origem_localizacao=DESCONHECIDA` | **400** |

**Verificação final da sessão (2026-08-13, antes do fechamento):**

- `uv run pytest` → **94 testes, todos passando, suíte limpa**.
- `manage.py check` → sem issues (0 silenced).
- `makemigrations --check --dry-run` → `No changes detected` (nenhuma
  migration pendente de gerar).
- Migrations `0001`–`0005` aplicadas no banco local.
- `captar --segmento SALAO --quadrante Q1 --dry-run` (Google, sem `--fonte`)
  → saída idêntica à da Fase 1, sem regressão.

### Estado real do banco local ao fechar

| Métrica | Valor |
|---|---|
| Prospects `origem=FOURSQUARE` | **400** |
| `status_funil` | `VERIFICAR_SITE: 400` (100% na fila) |
| `tem_site_real` | **`None`: 373** (desconhecido) · **`False`: 27** (checado) |
| `ativo_no_funil` | `False: 400` |
| `e_alvo == True` | **0** — nenhum qualificado automaticamente |
| Prospects `origem=GOOGLE_PLACES` | **0** (billing bloqueado, nunca captou) |
| Fila `ProspectVerificacao` | 400, ordenada por telefone: **158 com · 242 sem** |

`db.sqlite3` está no `.gitignore` — esses 400 são dado local, não vão para o
repo. Quem clonar o projeto começa com banco vazio.

## Foursquare — fonte alternativa (sessão de 2026-08-13)

### Por que entrou

O **billing do Google Cloud está bloqueado**. Chamado de suporte aberto, **sem
previsão de resolução** — não há data prometida nem canal de escalonamento
ativo. Sem billing, a Places API não responde, e a Fase 1 inteira fica parada
por dependência de uma única fonte. A Foursquare entrou para destravar o
trabalho, **não para substituir o Google**.

### Status: funciona, e não é substituição

Varredura real de Maringá em 2026-08-13, segmento SALAO, 16 quadrantes,
**19 requisições**: **408 estabelecimentos, 400 sem site, 395 prospects novos**.

Distribuição muito desigual — 5 dos 16 quadrantes vieram **zerados** (Q5, Q9,
Q13, Q14, Q16), e a massa está no centro (Q7 = 89, Q11 = 63, Q8 = 54). Isso é
esperado: a Foursquare mapeia bem região central e comercial, e some no
bairro.

### O número que decide: 27 de 400

**Só 6,8% dos prospects (27 de 400) têm QUALQUER website no dado da
Foursquare.** Os outros 373 vieram com o campo vazio, e o validador de site
registrou `sem website` — ou seja, ele não teve o que checar.

Isso é o problema central desta fonte, e é mais grave que a cobertura menor:

- O critério comercial da VITRE é "não tem site". Do Google, `websiteUri` é
  populado com frequência e as três camadas do validador fazem trabalho real.
- Da Foursquare, "sem site" quase sempre significa **"a Foursquare não sabe"**,
  não "o negócio não tem site". O filtro deixa de filtrar.
- Consequência prática: a fila de curadoria enche de falso positivo, e o custo
  sai do bolso em tempo manual, não em fatura.

Outras lacunas medidas no mesmo lote:

- **Telefone em só 158 de 400 (39,5%).** Sem telefone o lead não é acionável —
  a abordagem é manual e é por telefone/WhatsApp.
- **Nota e total de avaliações: zero prospects.** São campos pagos (ver
  abaixo), e o Admin ordena a fila de curadoria justamente por
  `total_avaliacoes`. Prospect da Foursquare entra na fila sem critério de
  prioridade.

**Veredito: manter como contingência e banco de teste do pipeline. Não é
substituição definitiva.** Quando o billing do Google destravar, o Google
volta a ser fonte primária sem nenhuma mudança de código — ele já é o default.

### Três armadilhas da API que custaram descoberta

1. **`query` não é text search.** Passar a frase que o Google entende
   (`"Salão de beleza em Maringá PR"`) devolve hotel, universidade e
   supermercado — a API casa por relevância difusa e ignora o segmento. E
   `query="salão de beleza"` com acento devolve **zero**. A busca foi trocada
   para **`fsq_category_ids`**, com o texto só como fallback. No mesmo
   quadrante: 41 resultados de lixo por texto → 20 salões reais por categoria.
   Os ids de categoria estão fixos em `foursquare.py` (o endpoint de taxonomia
   responde 404 nesta geração; os ids foram colhidos das próprias respostas).
2. **`rating` e `stats` são campos PAGOS.** Consomem crédito de API, não a
   franquia de chamadas. A conta está **sem crédito**, e pedi-los devolve
   `429 "no API credits remaining"` que derruba a **busca inteira**, não só a
   nota. Ficam desligados por padrão (`FOURSQUARE_CAMPOS_PRO=False`).
3. **A geração da API mudou.** A chave do `.env` é da geração atual
   (`places-api.foursquare.com`, auth `Bearer`); a v3 legada
   (`api.foursquare.com/v3`) responde **401** para ela. O cliente deriva o
   estilo de auth do host em `FOURSQUARE_API_BASE` e aceita `fsq_place_id` e
   `fsq_id`. Os campos `closed_bucket`/`closed_status` da v3 **não existem**
   mais — devolvem 400; o campo de fechamento é `date_closed`.

### Mudança de regra: fonte não confiável não qualifica sozinha

**Regra nova (2026-08-13):** prospect de fonte cujo campo `website` não é
confiável **não pode ser tratado como qualificado pelo critério "sem site"**.
Ele nasce em `StatusFunil.VERIFICAR_SITE` e só sai dali por confirmação
manual.

**Motivo.** O filtro "sem site = alvo" foi desenhado para o Google, onde
`websiteUri` é populado de verdade — vazio ali é evidência. Na Foursquare,
373 dos 400 (93%) vieram vazios porque **a fonte não tem o dado**. O sistema
estava gravando `tem_site_real=False` nesses casos, ou seja, afirmando um
fato que ninguém apurou, e `e_alvo` retornava `True` para todos eles.

O risco não era o código aprovar sozinho — a aprovação sempre foi manual
(`ProspectAdmin.aprovar_para_funil`). Era que os 373 palpites ficavam
**indistinguíveis** de prospect qualificado de verdade na mesma tela, e um
"selecionar tudo → aprovar" levaria os dois juntos.

**Como ficou:**

- `FonteDeProspects.SITE_CONFIAVEL` — a política vive na fonte, não espalhada
  em `if origem == "FOURSQUARE"`. Google `True`, Foursquare `False`.
- `tem_site_real=None` quando nada foi verificado. O campo já reservava NULL
  para "desconhecido"; agora ele é usado com esse significado. Os 27 que
  tiveram URL real checada (404, DNS morto, conteúdo vazio, perfil de
  terceiro) mantêm `False` — ali houve apuração de verdade.
- `aprovar_para_funil` recusa `VERIFICAR_SITE` e avisa quantos bloqueou.
- Fila própria no Admin: **«Fila de verificação de site»** (proxy model
  `ProspectVerificacao`), **ordenada por telefone preenchido primeiro**. Não
  usa `rating`/`total_avaliacoes` porque `FOURSQUARE_CAMPOS_PRO` segue False
  e os 400 estão com esses campos nulos — ordenar por eles seria ordem
  aleatória. Telefone (158 dos 400) é o único sinal de que vale gastar
  revisão manual: sem telefone não há como abordar nem se o lead se confirmar.
- Saídas da fila: "Confirmei: NÃO tem site" → volta a `NOVO` e segue a
  curadoria normal; "Verifiquei: TEM site" → `DESCARTADO`.

**Retroativo.** Migration `0005_foursquare_pendente_de_verificacao` moveu os
400 já gravados. Só `origem=FOURSQUARE` e só `revisado_manualmente=False` —
curadoria humana não é desfeita por migration. UPDATE puro: não recaptura,
não duplica, não toca em `GOOGLE_PLACES`. Reversibilidade **testada numa
cópia do banco**, ida e volta exatas: rollback devolve `{NOVO: 400,
tem_site_real False: 400}`, reaplicar devolve `{VERIFICAR_SITE: 400, None:
373, False: 27}`.

### Decisão fechada: manter os dois motivos separados na fila

**Os 27 "site checado, confirmar" NÃO são uniformizados com os 373
"desconhecido".** Decisão do operador, tomada nesta sessão, após a opção de
uniformizar ter sido colocada explicitamente. **Não reabrir.**

O que separa os dois grupos é se houve apuração:

| Grupo | `tem_site_real` | O que aconteceu | Tag na fila |
|---|---|---|---|
| **373** | `None` | A Foursquare não deu URL. **Nada foi verificado.** | `Foursquare · site desconhecido — verificar manualmente` |
| **27** | `False` | O validador seguiu uma URL real e reprovou (404, DNS morto, conteúdo vazio, perfil de terceiro). | `Foursquare · site checado, confirmar — verificar manualmente` |

**Motivo de manter separado:** `tem_site_real=False` nos 27 é **fato
apurado**, com evidência gravada em `site_evidencia`. Achatar isso para
`None` apagaria trabalho de verificação que o sistema já fez e obrigaria a
refazê-lo à mão. Os dois grupos vão para a mesma fila — ambos precisam de
confirmação humana — mas chegam lá com históricos diferentes, e a tag diz
qual é qual. Na prática os 27 são revisão mais rápida: já há evidência para
ler antes de decidir.

**Fluxo do Google: zero alteração.** Guardado por testes de regressão em
`tests/test_verificacao_site.py` — vazio continua qualificando, recaptura
continua sem tocar em `status_funil`, quem tem site continua fora do banco.

### Telegram: não construído, por decisão

O pedido original falava em "enviar ao bot do Telegram". **Não existe e nunca
existiu integração com Telegram neste repo** — zero referências a bot, token
ou chat_id (o único match de busca é `telegram.me` na blacklist de domínios,
que é outra coisa). Isso foi levantado antes de qualquer alteração.

**Decisão do operador:** fila no Django Admin agora, Telegram depois. Nada de
bot nesta sessão.

**O que fica pronto para o sender futuro:** `ProspectVerificacao`
(`leads/models.py`) já é exatamente a fila que um bot precisaria consumir —
recorte em `VERIFICAR_SITE` e ordenação telefone-primeiro já implementados em
`ProspectVerificacaoAdmin.get_queryset` / `get_ordering`. Um sender futuro lê
esse queryset e usa `prospect.tag_verificacao` como texto da etiqueta. Nada
precisa ser refeito, só plugado.

Ao construir: lembrar que a decisão congelada "**nenhum envio automático**"
fala de abordagem a prospect, não de aviso interno ao operador — mas a
distinção precisa ser explicitada antes de escrever a primeira linha.

### Franquia separada, de propósito

`Varredura.fonte` (migration `0003`) existe para que o consumo seja contado
**por fonte**. Sem isso, varredura na Foursquare descontaria da franquia do
Google e travaria a captação cedo. Tetos: Google 1.000/mês, Foursquare
10.000/mês (`FOURSQUARE_FRANQUIA_MENSAL`).

### Lacuna conhecida, não corrigida

`total_requisicoes` conta apenas requisições **bem-sucedidas**. Tentativa que
falha e é retentada (4x em 5xx/429) não entra no contador. Vale para as duas
fontes — é comportamento herdado do cliente do Google, não regressão. Numa
API que responde 429 por falta de crédito, isso subestima o gasto real. Se a
franquia da Foursquare virar restrição de verdade, corrigir antes.

## Precisão da captação (sessão de 2026-08-14)

Disparada por uma revisão manual que encontrou, na fila, um estabelecimento
permanentemente fechado.

### Fechados: o filtro já existia — a fonte é que não sabe

**O achado que muda o diagnóstico:** o corte de fechados nunca esteve
faltando. `_coletar` já descartava candidato com `date_closed` desde a
integração da Foursquare, e `date_closed` já estava no `fields` da requisição.
O salão fechado atravessou porque **a Foursquare não marcou aquele registro
como fechado**. Não é bug de código; é lacuna da base deles.

Confirmado contra a API real (geração `places-api.foursquare.com`,
`X-Places-Api-Version: 2025-06-17`), e vale registrar porque contradiz o
palpite óbvio:

- `date_closed` é o nome certo e é campo **gratuito**. `closed_bucket` e
  `closed_status` (nomes da v3) devolvem **400**.
- A API **valida** nome de campo — `campo_que_nao_existe` devolve 400. Ou
  seja: o campo estava mesmo sendo pedido, não silenciosamente ignorado.
- A resposta **omite** `date_closed` quando é nulo, em vez de mandar `null`.
- Nenhum dos resultados de Maringá amostrados trouxe o campo preenchido.

**Consequência prática:** `date_closed` vazio significa "a Foursquare não
afirma que fechou", e **nunca** "está aberto". Confirmar fechamento continua
sendo trabalho da revisão manual — o filtro automático só pega o caso fácil.

### O que mudou no código

- `Varredura.total_fechados` — contador próprio, separado de dedup e de
  banimento. Sem ele o descarte é silencioso, e "fonte devolvendo lixo
  fechado" fica indistinguível de "fonte com muita repetição". `captar`
  imprime o número quando é maior que zero.
- Google Places passou a cortar **`CLOSED_TEMPORARILY` além de
  `CLOSED_PERMANENTLY`** — antes só o permanente. Mudança deliberada de
  comportamento: `date_closed` da Foursquare não separa permanente de
  temporário, e critérios diferentes por fonte fariam a fila de uma parecer
  mais suja que a da outra sem motivo aparente.
- `ProspectCandidate.fechado_evidencia` guarda o que a fonte disse
  (`date_closed=2024-03-11`, `businessStatus=...`) e vai para o log.

### Os 400 já captados: limitação assumida

**Não é respondível offline.** O descarte por fechamento acontece na coleta,
antes da gravação: o candidato fechado nunca virou linha e nenhuma coluna
guardou o `date_closed` de quem passou. Não há histórico a reler.

`manage.py verificar_fechados` existe para isso, com o gasto explícito:

- **Sem flag:** não toca a rede, só explica a limitação acima.
- **`--consultar-api`:** 1 requisição por prospect no endpoint de detalhes
  (`/places/{id}`, verificado: 200 e gratuito). **Não recaptura** — pergunta
  por id que já está no banco, então não cria nem duplica prospect.
- **`--confirmar`:** sem isto, `--consultar-api` só estima o custo e para.
- **`--limite N`:** amostrar antes de gastar.
- O gasto é registrado como `Varredura` para a franquia não ficar furada.
- 404 (id sumiu da base) é marcado como **indício**, não descarte — a
  Foursquare também remove duplicata e registro ruim.

**Não foi executado nesta sessão**, conforme a instrução de não gastar
franquia com recaptura. Custo se for rodar: **400 requisições de 10.000**.
Dado o achado acima (a base quase não preenche o campo), a expectativa é que
devolva perto de zero — o valor real dele é medir esse "perto de zero".

> Nota de franquia: ~20 requisições foram gastas à mão nesta sessão para
> confirmar o comportamento da API, e não estão registradas em `Varredura`.
> O contador do mês está subestimado nesse tanto.

### Telefone: 108 dos 158 não eram celular

A fila contava "158 com telefone" olhando apenas se o campo estava vazio.
Medido nos 400 do banco local depois de classificar:

| Tipo | Qtd | Serve para abordagem? |
|---|---|---|
| `CELULAR` | **50** | sim |
| `FIXO` | 101 | não — não abre conversa por mensagem |
| `LEGADO` (celular pré-2016, sem o 9º dígito) | 4 | talvez, à mão |
| `INVALIDO` (sem DDD) | 3 | talvez, à mão |
| `VAZIO` | 242 | não |

**108 dos 158 "com telefone" não eram celular de formato válido.** A fila
priorizava uma centena de fixos à frente de leads acionáveis.

O que mudou:

- `leads/utils/telefone.py` — valida DDD (11–99) + 9 dígitos começando em 9,
  aceitando as variações de formatação (`+55`, parênteses, traço, espaços).
- `Prospect.telefone` agora é gravado normalizado em **`+55DDNNNNNNNNN`**.
- `Prospect.telefone_tipo` classifica o **formato**; a fila ordena por
  `CELULAR`, não por campo preenchido.
- Migração `0007` normalizou os 400 existentes. Reversível — ida/volta/ida
  testada, resultado idêntico.

**Duas coisas que o código não afirma, de propósito:**

1. **Não afirma que o número tem WhatsApp.** `telefone_tipo=CELULAR` é um
   juízo sobre a *string*, não sobre a linha. Confirmar conta ativa exigiria
   contatar número sem consentimento — vetado pelo protocolo, e não foi
   implementado.
2. **Não insere o 9º dígito nos 4 `LEGADO`.** Inventar dígito produz um
   número que disca para outra pessoa. Ficam marcados para revisão humana.

## Decisões tomadas

1. **Places API como fonte primária**, não dataset CNPJ. O critério comercial
   é "não tem site", e só o Places responde isso. CNPJ fica para
   enriquecimento futuro (nome do sócio, que o Google não dá).
2. **Nenhum envio automático.** Sem bot de WhatsApp. Regra não negociável.
3. **Segmento = termo de busca**, não CNAE nem `primaryType`. Elimina a
   heurística de palavra-chave para separar salão de barbearia.
4. **Django + SQLite, sem container e sem VPS.** Operação de uma pessoa.
   Infra só quando houver receita.
5. **Banimento denormalizado** (`Descarte.origem_id`): o veto sobrevive à
   exclusão do prospect. Falha encontrada por teste nesta sessão.
6. **Ordenação do Admin por `total_avaliacoes` desc**: negócio consolidado e
   sem site é a melhor porta de entrada.
7. **Fonte é plugável, Google continua default** (2026-08-13). `--fonte` no
   `captar`, contrato em `leads/sources/base.py`, registro em
   `leads/sources/__init__.py`. Omitir `--fonte` mantém o comportamento
   anterior byte a byte. As duas fontes não se conhecem e não compartilham
   franquia.
8. **Fonte não confiável não qualifica sozinha** (2026-08-13).
   `SITE_CONFIAVEL` na fonte; Foursquare nasce em `VERIFICAR_SITE`.
   "Sem site" só vale como qualificação onde a fonte popula `website`.
9. **Os 27 "site checado" ficam distintos dos 373 "desconhecido"**
   (2026-08-13). Uniformizar apagaria apuração já feita. **Não reabrir.**
10. **Telegram não construído** (2026-08-13). Fila no Admin;
    `ProspectVerificacao` pronto para um sender consumir depois.

## Calibrações que dependem de dado real

O filtro de site tem dois números que só fecham com o primeiro lote de
Maringá. Ambos já têm teste protegendo contra regressão:

- `_MAX_TEXTO_PARKED = 250` — acima disso, "em breve" num site legítimo
  viraria falso positivo. Baixado de 500 para 250 nesta sessão, por falha de
  teste.
- `blacklist.yml` — cobre social, link-in-bio, agendamento (Trinks, Booksy,
  Avec), agregadores e encurtadores. Ampliar conforme aparecerem casos reais:
  cada prospect traz `site_evidencia` gravada, que é o material de auditoria.

## Próxima sessão

### 1. PENDÊNCIA ABERTA — revisão manual da fila de verificação

**É o trabalho que destrava tudo o mais, e é 100% humano.** Os 400 prospects
da Foursquare estão parados em `VERIFICAR_SITE` e nenhum avança sem alguém
olhar. Nada de código é necessário para começar: a tela está pronta.

**Onde:** Django Admin → **«Fila de verificação de site»**
(`ProspectVerificacao`). Já vem filtrada e ordenada.

**Ordem de ataque — começar pelos 50 com celular.** (Era "158 com telefone"
até 2026-08-14; ver a seção de precisão abaixo — 101 daqueles 158 eram fixos
e não abrem conversa por mensagem.) Eles estão no topo da fila por
construção. O motivo é econômico: sem celular não há como abordar o lead nem
se ele se confirmar sem site, então revisar os demais primeiro seria gastar o
recurso escasso (seu tempo) no material de menor retorno. Os 27 com tag "site
checado, confirmar" são os mais rápidos — já há `site_evidencia` gravada para
ler antes de decidir.

Depois dos 50, a ordem de retorno decrescente é: **4 LEGADO** (celular antigo,
provável que só falte o 9º dígito — vale conferir à mão), **3 INVALIDO**
(número sem DDD, dá para deduzir pelo endereço), **101 FIXO** e por fim os
**242 sem telefone nenhum**.

**Como decidir cada linha:** abrir o nome do estabelecimento no Google/
Instagram e responder uma pergunta só — tem site próprio?

- **Não tem** → ação "Confirmei: NÃO tem site" → vai para `NOVO` e entra na
  curadoria normal, igual a um lead do Google.
- **Tem** → ação "Verifiquei: TEM site" → `DESCARTADO`.

Ambas gravam `revisado_manualmente=True`, então recaptura futura não desfaz
a decisão.

**O número a extrair da revisão:** de cada 100 revisados, quantos tinham site
de verdade? Essa é a **taxa de falso positivo da Foursquare**, e é ela que
decide se a fonte serve para prospecção real ou se fica só como banco de
teste do pipeline. Anotar o resultado aqui na próxima sessão.

### 2. Resto da fila de trabalho

1. **Destravar o billing do Google** — segue sendo o caminho principal. Sem
   previsão do suporte; enquanto isso a Foursquare cobre o pipeline.
2. Rodar os outros 4 segmentos na Foursquare (`ESTETICA`, `NAIL`, `LASH`,
   `SOBRANCELHA`). Atenção: LASH e SOBRANCELHA caem na categoria genérica de
   beleza — a Foursquare não tem categoria própria para eles, então o ruído
   será maior.
3. Quando o Google voltar: `manage.py captar --segmento SALAO` (sem `--fonte`)
   e comparar os dois lotes no Admin, filtrando por fonte no mesmo quadrante.
   É a medida direta de cobertura Google x Foursquare.
4. Auditar `site_evidencia` e recalibrar a blacklist — com dado do Google,
   que é onde o validador tem URL para checar.
5. Fase 2: tela de Kanban própria (hoje o funil vive no Admin) e
   enriquecimento por CNPJ para nome do sócio. O sender de Telegram, se
   entrar, consome `ProspectVerificacao` (ver acima).

## Pendências de protocolo

- **Commit inicial ainda NÃO feito.** O repositório tem **zero commits** —
  `git log` responde "does not have any commits yet". Os **46 arquivos** do
  projeto estão todos como untracked. Commits são GPG-assinados em terminal
  externo, nunca dentro do agente. Rodar manualmente.
- `.env` está no `.gitignore` (conferido: `git check-ignore` confirma) e
  `db.sqlite3` também — os 400 prospects são dado local e não vão ao repo.
  Reconferir antes do primeiro push.
- `scripts/save-session.sh` e `scripts/health-check.sh` do sv-protocol
  **não existem neste repo** (nem em `~/.claude/scripts/`). O health check de
  início de sessão foi feito na mão: `git log` / `git status` / `git diff`.
  Criar os scripts é item de protocolo em aberto.


---

# ATUALIZAÇÃO OPERACIONAL — 2026-09-02

> Esta seção representa o estado atual do projeto e prevalece sobre trechos
> históricos anteriores deste documento quando houver divergência.

## CRM manual de prospecção — operacional

O Django Admin deixou de ser apenas uma fila de verificação e agora também
funciona como mini-CRM operacional para prospecção manual da VITRE.

A regra central permanece inalterada:

**NENHUMA mensagem é enviada automaticamente.**

O sistema apenas prepara a mensagem e abre o WhatsApp. O envio continua
dependendo de ação humana explícita.

### Segmentos

Foi incluído:

- `BARBEARIA`

A Foursquare foi ajustada para não misturar mais barbearias com `SALAO`.

Mapeamento atual relevante:

- `SALAO` → Hair Salon + categoria genérica de beleza
- `BARBEARIA` → Barbershop

Migration relacionada:

- `0008_alter_prospect_segmento_alter_varredura_segmento.py`

## Status comerciais atuais

O funil agora possui:

- `VERIFICAR_SITE` — Verificar site (pendente)
- `NOVO` — Novo (curadoria)
- `INICIAR` — A iniciar
- `EM_ANDAMENTO` — Em andamento
- `SEM_RESPOSTA` — Sem resposta
- `NUMERO_INVALIDO` — Número inválido
- `REMARKETING` — Remarketing
- `CONVERTIDO` — Convertido
- `DESCARTADO` — Descartado

Migrations relacionadas:

- `0009_alter_prospect_status_funil.py`
- `0010_alter_prospect_status_funil.py`

### Semântica operacional

`EM_ANDAMENTO`
: cliente respondeu ou existe conversa comercial ativa.

`SEM_RESPOSTA`
: primeira abordagem foi realizada, número é utilizável, mas não houve resposta.

`NUMERO_INVALIDO`
: número incorreto, inexistente ou inútil para a abordagem.

`REMARKETING`
: ciclo inicial esfriou, mas o lead ainda pode ser retomado futuramente.

`CONVERTIDO`
: venda fechada.

`DESCARTADO`
: lead encerrado comercialmente.

Os status terminais:

- `CONVERTIDO`
- `DESCARTADO`
- `NUMERO_INVALIDO`

fazem:

- `ativo_no_funil=False`
- `proximo_contato_em=None`

Logo, não permanecem gerando follow-up.

## Interações como mini-CRM

A listagem de `Interacao` agora permite operar o funil sem abrir cada registro.

Recursos implementados:

- dropdown editável de segmento;
- dropdown editável de status;
- cores diferentes por status;
- alteração salva diretamente no `Prospect`;
- filtro por canal;
- filtro por segmento;
- filtro por status;
- botão para WhatsApp;
- botão `Enviar modelo`;
- botão `Enviar proposta`;
- coluna visual de próximo retorno.

As alterações manuais também marcam:

- `revisado_manualmente=True`

para evitar que uma recaptura futura desfaça a curadoria humana.

## Follow-up

`Prospect.proximo_contato_em` passou a ser utilizado operacionalmente.

Na listagem de Interações:

- retorno futuro → data normal;
- retorno amanhã → badge amarelo;
- retorno hoje → destaque;
- retorno vencido → badge vermelho;
- status terminal → `—`.

Existe alerta no topo quando há contatos vencidos.

O alerta possui link **Ver agora**, que aplica o filtro:

- `Retorno pendente`

Também existem filtros para:

- retorno pendente;
- retorno agendado;
- sem data de retorno.

## WhatsApp

### Regra

O Django **não envia WhatsApp**.

Ele somente:

1. identifica o número;
2. gera a copy adequada à etapa;
3. monta a URL `wa.me`;
4. abre o WhatsApp;
5. o operador revisa;
6. o operador envia manualmente.

Os links passaram a abrir em nova aba (`target="_blank"`), mantendo o Django
aberto como painel operacional.

Fluxo desejado:

- Aba 1 → Django Admin
- Aba 2 → WhatsApp

## Fluxo comercial atual

### Etapa 1 — primeira abordagem

A abordagem deixou de ser um bloco comercial com preço + link + benefícios.

Agora a mensagem é curta, conversacional e explica claramente que o serviço
oferecido é um **site**, evitando o termo amplo “presença digital”.

Exemplo conceitual:

- apresentação breve;
- descoberta do negócio;
- informação de que desenvolve sites para negócios locais;
- benefício contextual;
- pergunta: “Posso te mandar o link também?”

O operador envia manualmente junto com um print do template adequado.

Não há preço na primeira mensagem.

Não há link automaticamente na primeira mensagem.

### Etapa 2 — cliente aceitou ver

Quando o cliente responde algo como:

- “manda”
- “pode”
- “quero ver”
- “como fica?”

o lead passa para:

- `EM_ANDAMENTO`

Na tabela aparece:

- `Enviar modelo`

Templates atuais:

Barbearia:
`https://vitre-storefront.salvatiniguilherme.workers.dev/barber-demo`

Beleza / ÉLARA:
`https://d2604417.vitre-estetica-01-elara.pages.dev/`

A mensagem explica que o modelo é demonstrativo e que a versão final será
personalizada para o negócio.

### Etapa 3 — gostou / perguntou preço

Para lead em `EM_ANDAMENTO`, existe:

- `Enviar proposta`

Oferta atual:

- site estático: R$ 59,90/mês
- site animado: R$ 69,90/mês
- anual: R$ 500/ano
- domínio separado

A copy permanece curta e conversacional.

### Sem resposta

`SEM_RESPOSTA` possui mensagem própria de segunda abordagem.

Ela não repete toda a apresentação inicial.

O objetivo é lembrar o contato anterior e retomar a conversa de maneira curta.

### Em andamento

Também existe mensagem de continuidade para conversas que já começaram.

## Registro histórico — estado técnico validado

Validação executada após as alterações:

```text
python manage.py check
System check identified no issues (0 silenced).
```

Suíte de testes:

```text
137 passed
```

Executada novamente após as alterações principais e permaneceu verde.

Também validado:

```text
python manage.py makemigrations --check
No changes detected
```

Migrations aplicadas localmente:

- `0001` a `0010` — todas aplicadas.

## Arquivos alterados nesta rodada

Versionados modificados:

- `leads/admin.py`
- `leads/models.py`
- `leads/sources/foursquare.py`

Novas migrations:

- `leads/migrations/0008_alter_prospect_segmento_alter_varredura_segmento.py`
- `leads/migrations/0009_alter_prospect_status_funil.py`
- `leads/migrations/0010_alter_prospect_status_funil.py`

Backups temporários `leads/admin.py.bak-*` foram removidos antes do fechamento.

## Débito técnico identificado

`leads/admin.py` chegou a aproximadamente **1.219 linhas**.

Não refatorar antes de validar comercialmente o fluxo.

Depois da primeira rodada real de uso, considerar extrair:

- geração de copies;
- URLs de WhatsApp;
- regras de follow-up;
- componentes/helpers do Admin;
- lógica comercial específica por segmento.

Objetivo da futura refatoração:

- reduzir responsabilidade do `admin.py`;
- evitar duplicação de `_mensagem_whatsapp`;
- facilitar testes do fluxo comercial;
- manter comportamento atual sem regressão.

Não existe, até esta atualização, regra formal encontrada no repositório determinando limite de linhas por arquivo. A refatoração é uma decisão de manutenibilidade, não requisito documental atual.

## Situação real do Git

A informação histórica anterior de que o repositório possuía zero commits está desatualizada.

Estado verificado em 2026-09-02:

```text
bbb1de8 feat: filtro de fechados (Foursquare+Google) e validação de telefone celular BR
9086743 feat: bootstrap Fase 1 + fonte Foursquare com fila de verificação manual
```

Branch atual:

```text
master
```

Portanto já existem **2 commits** no histórico.

Commits continuam sendo feitos manualmente e assinados via GPG no terminal. O agente não deve criar commits automaticamente.

## Próximos blocos — NÃO IMPLEMENTADOS

### Responsividade mobile

Próxima evolução planejada:

- adaptar o Django Admin para operação confortável em celular;
- priorizar lista de interações, status, retorno e botões comerciais;
- manter fluxo utilizável em telas pequenas.

### Servidor online

Depois da responsividade, avaliar hospedagem do Django para permitir operação fora do computador local.

Objetivo:

- acessar o CRM de qualquer lugar;
- prospectar pelo celular ou notebook;
- não depender de `127.0.0.1`.

A arquitetura/hospedagem ainda não foi decidida.

Não criar VPS/infra antes dessa decisão.

## Prioridade operacional imediata

O sistema já está suficiente para iniciar prospecção real.

Prioridade agora:

1. utilizar a fila;
2. realizar abordagens manualmente;
3. registrar respostas;
4. usar follow-ups;
5. medir taxa de resposta;
6. medir quantos pedem o modelo;
7. medir quantos perguntam preço;
8. medir conversões.

Evitar continuar adicionando funcionalidades sem necessidade observada no uso real.
