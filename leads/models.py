"""Modelo de dados do vitre-leads-engine.

Cinco entidades:

- `Quadrante`  — célula da grade geográfica de uma cidade. A captação roda por
  quadrante para superar o teto de 60 resultados por busca da Places API.
- `Varredura`  — uma execução de captação (termo x quadrante). Guarda o custo
  real em requisições, que é a unidade de cobrança da franquia.
- `Prospect`   — o estabelecimento captado. Unicidade por (origem, origem_id).
- `Socio`      — sócio/administrador, vindo do enriquecimento por CNPJ.
- `Interacao`  — registro de contato manual (o sistema nunca envia nada).
- `Descarte`   — motivo da saída do funil; com `banido`, impede recaptura.

Regra central de recaptura: campos revisados à mão nunca são sobrescritos por
uma nova varredura (ver `Prospect.revisado_manualmente`).
"""

from __future__ import annotations

import uuid

from django.db import models

from leads.utils.telefone import TIPO_TELEFONE_CHOICES, TipoTelefone


class Segmento(models.TextChoices):
    """Segmentos-alvo da VITRE.

    Diferente do CNAE (que junta salão e barbearia em 9602-5/01) e do
    `primaryType` do Google (grosseiro demais), o segmento aqui é derivado do
    TERMO DE BUSCA que capturou o prospect — ver `Varredura.segmento`. Isso
    dispensa heurística de palavra-chave sobre o nome fantasia.
    """

    SALAO = "SALAO", "Salão de beleza"
    BARBEARIA = "BARBEARIA", "Barbearia"
    ESTETICA = "ESTETICA", "Clínica de estética"
    NAIL = "NAIL", "Nail designer"
    LASH = "LASH", "Lash designer"
    SOBRANCELHA = "SOBRANCELHA", "Design de sobrancelha"
    OUTRO = "OUTRO", "Outro"


class Origem(models.TextChoices):
    """Fonte que trouxe o prospect. `origem_id` é o identificador nativo dela."""

    GOOGLE_PLACES = "GOOGLE_PLACES", "Google Places"
    FOURSQUARE = "FOURSQUARE", "Foursquare Places"
    CNPJ = "CNPJ", "Dataset CNPJ (Receita Federal)"
    MANUAL = "MANUAL", "Cadastro manual"


# Nome curto da fonte para a tag da fila de verificação. O label completo
# ("Foursquare Places") é verboso demais para uma etiqueta de lista.
ROTULO_CURTO_ORIGEM = {
    Origem.GOOGLE_PLACES: "Google",
    Origem.FOURSQUARE: "Foursquare",
    Origem.CNPJ: "CNPJ",
    Origem.MANUAL: "Manual",
}


class StatusFunil(models.TextChoices):
    """Colunas do funil. NOVO fica fora do board (fila de curadoria).

    CONVERTIDO e DESCARTADO são terminais: tiram o prospect do funil ativo.

    VERIFICAR_SITE também fica fora do board, e é ANTES do NOVO: prospect
    cuja fonte não permite afirmar "não tem site". Existe porque a Foursquare
    devolve `website` vazio em 93% dos casos por não ter o dado — o que não
    é a mesma coisa que o negócio não ter site. Sem esse estado, palpite e
    prospect qualificado ficam indistinguíveis na fila.
    """

    VERIFICAR_SITE = "VERIFICAR_SITE", "Verificar site (pendente)"
    NOVO = "NOVO", "Novo (curadoria)"
    INICIAR = "INICIAR", "A iniciar"
    EM_ANDAMENTO = "EM_ANDAMENTO", "Em andamento"
    SEM_RESPOSTA = "SEM_RESPOSTA", "Sem resposta"
    NUMERO_INVALIDO = "NUMERO_INVALIDO", "Número inválido"
    REMARKETING = "REMARKETING", "Remarketing"
    CONVERTIDO = "CONVERTIDO", "Convertido"
    DESCARTADO = "DESCARTADO", "Descartado"


class StatusVarredura(models.TextChoices):
    PENDENTE = "PENDENTE", "Pendente"
    RODANDO = "RODANDO", "Rodando"
    CONCLUIDA = "CONCLUIDA", "Concluída"
    ERRO = "ERRO", "Erro"


class Quadrante(models.Model):
    """Célula retangular da grade de uma cidade.

    Gerada por `leads.services.grade.gerar_grade` a partir do bbox geocodado.
    Os quatro cantos alimentam `locationRestriction.rectangle` da Places API.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    cidade = models.CharField(max_length=100)
    estado = models.CharField(max_length=2)
    rotulo = models.CharField(max_length=10, help_text="Q1..Qn, Q1 no canto sudoeste")

    sul = models.FloatField()
    oeste = models.FloatField()
    norte = models.FloatField()
    leste = models.FloatField()

    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "quadrante"
        verbose_name_plural = "quadrantes"
        constraints = [
            models.UniqueConstraint(
                fields=["cidade", "estado", "rotulo"],
                name="uniq_quadrante_por_cidade",
            )
        ]
        ordering = ["cidade", "rotulo"]

    def __str__(self) -> str:
        return f"{self.cidade}/{self.estado} {self.rotulo}"


class Varredura(models.Model):
    """Uma execução de captação: um termo de busca dentro de um quadrante.

    `total_requisicoes` é gravado mesmo em caso de erro parcial — requisições
    já consumidas contam na franquia ainda que a varredura termine em ERRO.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    termo_busca = models.CharField(
        max_length=200,
        help_text='Texto literal enviado à API, ex: "salão de beleza em Maringá PR"',
    )
    segmento = models.CharField(max_length=20, choices=Segmento.choices)
    # Cada fonte tem franquia própria, então o consumo é contado POR fonte —
    # sem este campo, uma varredura na Foursquare descontaria da franquia do
    # Google. Default = GOOGLE_PLACES: toda varredura anterior a este campo é
    # do Google, e é o que o comando usa quando `--fonte` é omitido.
    fonte = models.CharField(
        max_length=20,
        choices=Origem.choices,
        default=Origem.GOOGLE_PLACES,
        help_text="Fonte consultada nesta varredura.",
    )
    cidade = models.CharField(max_length=100)
    estado = models.CharField(max_length=2)
    quadrante = models.ForeignKey(
        Quadrante,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="varreduras",
    )

    status = models.CharField(
        max_length=20,
        choices=StatusVarredura.choices,
        default=StatusVarredura.PENDENTE,
    )

    # Contadores. `total_requisicoes` é a unidade de cobrança da franquia.
    total_requisicoes = models.PositiveIntegerField(default=0)
    total_encontrados = models.PositiveIntegerField(default=0)
    total_sem_site = models.PositiveIntegerField(default=0)
    total_novos = models.PositiveIntegerField(default=0)
    # Descartados por a fonte declarar o estabelecimento fechado. Contador
    # SEPARADO de dedup e de banimento de propósito: os três somem da fila
    # pelo mesmo lugar, e sem separá-los uma fonte que comece a devolver lixo
    # fechado parece apenas uma fonte com muita repetição.
    # `total_encontrados` conta o que sobrou depois deste corte.
    total_fechados = models.PositiveIntegerField(
        default=0,
        help_text="Descartados na captação por estarem fechados na fonte.",
    )

    erro = models.TextField(blank=True, default="")

    criado_em = models.DateTimeField(auto_now_add=True)
    concluido_em = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "varredura"
        verbose_name_plural = "varreduras"
        ordering = ["-criado_em"]
        indexes = [models.Index(fields=["-criado_em"])]

    def __str__(self) -> str:
        alvo = self.quadrante.rotulo if self.quadrante else "cidade inteira"
        return f"{self.termo_busca} @ {alvo} ({self.status})"


class Prospect(models.Model):
    """Estabelecimento captado — o lead.

    Unicidade por (origem, origem_id): `place_id` para Places, CNPJ para o
    dataset da Receita. Assim as duas fontes convivem sem colidir, e a
    recaptura reusa o registro em vez de duplicar.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    origem = models.CharField(max_length=20, choices=Origem.choices)
    origem_id = models.CharField(
        max_length=255,
        help_text="place_id (Places) ou CNPJ (Receita)",
    )

    nome = models.CharField(max_length=255)
    razao_social = models.CharField(max_length=255, blank=True, default="")
    nome_fantasia = models.CharField(max_length=255, blank=True, default="")
    cnpj = models.CharField(max_length=14, blank=True, default="", db_index=True)

    segmento = models.CharField(
        max_length=20,
        choices=Segmento.choices,
        default=Segmento.OUTRO,
    )
    # Termo literal que achou o prospect na PRIMEIRA captura. Não é
    # sobrescrito em recaptura — responde "o que trouxe este lead até aqui".
    termo_busca = models.CharField(max_length=200, blank=True, default="")

    endereco = models.TextField(blank=True, default="")
    bairro = models.CharField(max_length=120, blank=True, default="")
    cidade = models.CharField(max_length=100, blank=True, default="")
    estado = models.CharField(max_length=2, blank=True, default="")

    # Normalizado para `+55DDNNNNNNNNN` na gravação (leads.utils.telefone).
    # Número que não casou com nenhum formato conhecido fica como veio — some
    # da contagem de acionáveis, mas continua visível para revisão manual.
    telefone = models.CharField(max_length=30, blank=True, default="")
    # FORMATO do telefone, não estado da linha. CELULAR aqui significa "o
    # texto tem formato de celular BR", e nada além disso: se a linha existe,
    # se atende ou se tem WhatsApp só se sabe contatando, o que o protocolo
    # não faz automaticamente.
    telefone_tipo = models.CharField(
        max_length=10,
        choices=TIPO_TELEFONE_CHOICES,
        default=TipoTelefone.VAZIO,
        db_index=True,
        help_text="Formato do número. Não afirma que a linha existe ou tem WhatsApp.",
    )
    instagram = models.CharField(max_length=255, blank=True, default="")
    website_url = models.TextField(blank=True, default="")

    # Marca de fechamento vinda da própria fonte (`date_closed` na Foursquare,
    # `businessStatus` no Google). Vazio = a fonte não disse que fechou, o que
    # NÃO é o mesmo que dizer que está aberto: a base da Foursquare tem o
    # campo, mas quase nunca o preenche para pequeno negócio no Brasil.
    # Captação nova nem chega a criar prospect fechado; este campo serve à
    # recheca retroativa (`manage.py verificar_fechados`).
    fechado_na_fonte = models.CharField(
        max_length=60,
        blank=True,
        default="",
        help_text="Evidência de fechamento vinda da fonte. Vazio = a fonte não afirmou nada.",
    )

    # Resultado do filtro de qualificação (leads.filters.site_validator).
    # None = ainda não avaliado. False = SEM site real → é o alvo da VITRE.
    tem_site_real = models.BooleanField(null=True, blank=True)
    site_evidencia = models.TextField(blank=True, default="")

    # Sinais de porte, usados para priorizar quem abordar primeiro:
    # muita avaliação + nenhum site = negócio consolidado e desatendido.
    rating = models.FloatField(null=True, blank=True)
    total_avaliacoes = models.PositiveIntegerField(null=True, blank=True)

    status_funil = models.CharField(
        max_length=20,
        choices=StatusFunil.choices,
        default=StatusFunil.NOVO,
    )
    ativo_no_funil = models.BooleanField(
        default=False,
        help_text="False enquanto está na curadoria ou já em coluna terminal",
    )
    proximo_contato_em = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Vencido = reaparece na tela de follow-up",
    )

    # Trava anti-sobrescrita: ligada assim que você edita segmento/status à
    # mão. A recaptura respeita e não desfaz curadoria já feita.
    revisado_manualmente = models.BooleanField(default=False)

    varredura = models.ForeignKey(
        Varredura,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="prospects",
    )

    criado_em = models.DateTimeField(auto_now_add=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "prospect"
        verbose_name_plural = "prospects"
        ordering = ["-criado_em"]
        constraints = [
            models.UniqueConstraint(
                fields=["origem", "origem_id"],
                name="uniq_prospect_por_origem",
            )
        ]
        indexes = [
            models.Index(fields=["status_funil", "ativo_no_funil"]),
            models.Index(fields=["tem_site_real"]),
            models.Index(fields=["proximo_contato_em"]),
        ]

    def __str__(self) -> str:
        return self.nome

    @property
    def pendente_de_verificacao(self) -> bool:
        """True enquanto ninguém confirmou à mão a situação do site."""
        return self.status_funil == StatusFunil.VERIFICAR_SITE

    @property
    def telefone_e_celular(self) -> bool:
        """True quando o telefone tem FORMATO de celular BR.

        Não afirma que a linha existe, que atende ou que tem WhatsApp — isso
        exigiria contatar o número sem consentimento.
        """
        return self.telefone_tipo == TipoTelefone.CELULAR

    @property
    def e_alvo(self) -> bool:
        """True quando o prospect passou no filtro: não tem site próprio.

        Pendente de verificação NUNCA é alvo: o `tem_site_real=False` de uma
        fonte que não popula `website` é ausência de dado, não qualificação.
        """
        if self.pendente_de_verificacao:
            return False
        return self.tem_site_real is False

    @property
    def tag_verificacao(self) -> str:
        """Etiqueta visível da fila de verificação. Vazia fora dela."""
        if not self.pendente_de_verificacao:
            return ""

        fonte = ROTULO_CURTO_ORIGEM.get(self.origem, self.origem)
        motivo = (
            "site desconhecido"
            if self.tem_site_real is None
            else "site checado, confirmar"
        )
        return f"{fonte} · {motivo} — verificar manualmente"


class ProspectVerificacao(Prospect):
    """Mesma tabela de `Prospect`, recortada na fila de verificação.

    Proxy — não cria tabela nem duplica linha. Existe para dar à fila da
    Foursquare uma tela própria, com ordenação própria (telefone primeiro),
    sem encostar no `ProspectAdmin` do fluxo do Google. Isolar assim é o que
    garante zero regressão na curadoria que já funciona.
    """

    class Meta:
        proxy = True
        verbose_name = "fila de verificação de site"
        verbose_name_plural = "fila de verificação de site"


class Socio(models.Model):
    """Sócio/administrador — vem do dataset CNPJ, não do Google.

    É o que permite abrir a conversa pelo nome da pessoa em vez do nome
    do estabelecimento.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    prospect = models.ForeignKey(
        Prospect,
        on_delete=models.CASCADE,
        related_name="socios",
    )
    nome = models.CharField(max_length=255)
    qualificacao = models.CharField(max_length=120, blank=True, default="")

    class Meta:
        verbose_name = "sócio"
        verbose_name_plural = "sócios"
        constraints = [
            models.UniqueConstraint(
                fields=["prospect", "nome"],
                name="uniq_socio_por_prospect",
            )
        ]

    def __str__(self) -> str:
        return self.nome


class Canal(models.TextChoices):
    WHATSAPP = "WHATSAPP", "WhatsApp (manual)"
    INSTAGRAM = "INSTAGRAM", "Instagram (manual)"
    TELEFONE = "TELEFONE", "Telefone"
    PRESENCIAL = "PRESENCIAL", "Presencial"
    EMAIL = "EMAIL", "E-mail"


class Interacao(models.Model):
    """Contato registrado à mão. O sistema não dispara mensagem em canal nenhum."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    prospect = models.ForeignKey(
        Prospect,
        on_delete=models.CASCADE,
        related_name="interacoes",
    )
    canal = models.CharField(max_length=20, choices=Canal.choices)
    anotacao = models.TextField()
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "interação"
        verbose_name_plural = "interações"
        ordering = ["-criado_em"]

    def __str__(self) -> str:
        return f"{self.get_canal_display()} — {self.prospect.nome}"


class MotivoDescarte(models.TextChoices):
    JA_TEM_SITE = "JA_TEM_SITE", "Já tem site"
    SEM_INTERESSE = "SEM_INTERESSE", "Sem interesse"
    SEM_VERBA = "SEM_VERBA", "Sem verba"
    FECHADO = "FECHADO", "Estabelecimento fechado"
    FORA_DO_PERFIL = "FORA_DO_PERFIL", "Fora do perfil (ex: barbearia)"
    SEM_RESPOSTA = "SEM_RESPOSTA", "Sem resposta após tentativas"
    OUTRO = "OUTRO", "Outro"


class Descarte(models.Model):
    """Saída do funil com motivo. `banido` impede recaptura futura.

    `origem_id` é copiado do prospect na gravação de propósito: o banimento
    precisa sobreviver à exclusão do prospect. Sem essa cópia, apagar o
    registro apagaria junto o veto (CASCADE) e a próxima varredura
    ressuscitaria calmamente o que você tinha barrado.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    prospect = models.ForeignKey(
        Prospect,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="descartes",
    )
    origem_id = models.CharField(
        max_length=255,
        blank=True,
        default="",
        db_index=True,
        help_text="Cópia do identificador de origem — mantém o veto se o prospect sumir",
    )
    motivo = models.CharField(max_length=20, choices=MotivoDescarte.choices)
    observacao = models.TextField(blank=True, default="")
    banido = models.BooleanField(
        default=False,
        help_text="Não retorna ao funil nem em nova varredura",
    )
    criado_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "descarte"
        verbose_name_plural = "descartes"
        ordering = ["-criado_em"]

    def save(self, *args, **kwargs):
        if not self.origem_id and self.prospect_id:
            self.origem_id = self.prospect.origem_id
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        nome = self.prospect.nome if self.prospect else self.origem_id
        return f"{self.get_motivo_display()} — {nome}"
