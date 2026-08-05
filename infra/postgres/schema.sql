-- Schema do indice de politicas.
--
-- Executado uma unica vez, na primeira subida do container (o Postgres so roda
-- os scripts de /docker-entrypoint-initdb.d quando o diretorio de dados esta
-- vazio). Para reaplicar: `docker compose down -v && docker compose up -d`.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS trecho_politica (
    id              TEXT PRIMARY KEY,
    politica_id     TEXT        NOT NULL,
    versao          TEXT        NOT NULL,
    secao           TEXT        NOT NULL,
    titulo_politica TEXT        NOT NULL,
    caminho_secao   TEXT[]      NOT NULL DEFAULT '{}',
    texto           TEXT        NOT NULL,
    produtos        TEXT[]      NOT NULL DEFAULT '{}',
    area            TEXT        NOT NULL DEFAULT '',
    vigencia_inicio DATE,
    -- 1024 dimensoes = intfloat/multilingual-e5-large. Trocar de modelo exige
    -- ALTER da coluna e reingestao completa; o adapter valida a dimensao na
    -- escrita para que a incompatibilidade apareca na hora, e nao como
    -- resultado de busca silenciosamente ruim.
    embedding       vector(1024) NOT NULL,
    atualizado_em   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Filtro por produto usa operador de array; GIN e o indice adequado.
CREATE INDEX IF NOT EXISTS idx_trecho_produtos ON trecho_politica USING gin (produtos);
CREATE INDEX IF NOT EXISTS idx_trecho_politica_id ON trecho_politica (politica_id);

-- HNSW para similaridade de cosseno.
--
-- Com algumas dezenas de trechos o planner faz seq scan e o indice nem e
-- usado — e isso esta correto. Ele existe para o corpus real (milhares de
-- trechos), onde a diferenca entre O(n) e O(log n) aparece. Construir agora
-- evita descobrir na producao que faltava indice.
--
-- vector_cosine_ops porque normalizamos os vetores; m e ef_construction nos
-- padroes, que ja atendem esta escala.
CREATE INDEX IF NOT EXISTS idx_trecho_embedding
    ON trecho_politica
    USING hnsw (embedding vector_cosine_ops);


-- =========================================================================
-- Analise de credito (Camada 9)
--
-- ## Por que estas tabelas existem, e o que elas destravam
--
-- Ate a Camada 8 a analise vivia num dicionario de processo. Isso bloqueava tres
-- coisas de uma vez: o trabalhador de extracao como processo separado (cada
-- processo veria o proprio dicionario), mais de uma replica da API, e a
-- durabilidade da propria analise — que sumia no restart.
--
-- ## O agregado e a fronteira da transacao
--
-- `analise` e a raiz; `documento`, `dado_extraido` e o parecer sao parte dela.
-- Gravar tudo numa transacao e o que impede um documento anexado sem o parecer
-- correspondente, ou dado extraido apontando para documento que nao foi gravado.
--
-- O parecer fica em colunas da propria `analise` e nao em tabela separada: ele e
-- 1-para-1 e nunca e consultado sem a analise. Tabela propria seria um JOIN em
-- toda leitura para nada.
CREATE TABLE IF NOT EXISTS analise (
    id                  UUID PRIMARY KEY,

    -- ## Bloqueio otimista, e o bug que ele impede
    --
    -- Com repositorio compartilhado, API e trabalhador escrevem o **mesmo**
    -- agregado. A corrida e concreta:
    --
    --   1. API carrega (v3), anexa o segundo documento, grava -> v4
    --   2. trabalhador tinha carregado v3 antes, aplica a extracao do primeiro,
    --      grava -> sobrescreve, e o segundo documento **desaparece**
    --
    -- Ninguem veria: nao ha erro, e o cliente so notaria que o documento que ele
    -- enviou nao esta la. Com a versao no `WHERE`, a segunda gravacao afeta zero
    -- linhas, o repositorio levanta, e o trabalhador recarrega e reaplica — o que
    -- e seguro porque `AplicarExtracao` e idempotente.
    --
    -- Bloqueio **otimista** e nao `SELECT FOR UPDATE`: conflito aqui e raro (exige
    -- upload simultaneo a extracao da mesma analise) e a extracao segura a linha
    -- por segundos. Pessimista transformaria um caso raro em espera constante.
    versao              INTEGER     NOT NULL DEFAULT 1,

    status              TEXT        NOT NULL,
    erro                TEXT,
    reavaliacoes        INTEGER     NOT NULL DEFAULT 0,
    motivo_reavaliacao  TEXT,

    -- Solicitante, achatado na raiz: e um value object 1-para-1, e normalizar
    -- daria uma tabela com uma linha por analise.
    solicitante_nome    TEXT        NOT NULL,
    -- CPF sem pontuacao, como o value object guarda. **Sem indice**: nao ha
    -- consulta por CPF na API, e um indice aqui convidaria a criar uma — busca
    -- por CPF e o caminho por onde um vazamento vira uma lista.
    solicitante_cpf     TEXT        NOT NULL,
    solicitante_nascimento TIMESTAMPTZ NOT NULL,
    -- NUMERIC e nao DOUBLE PRECISION. O dominio usa `Decimal` de proposito, e
    -- float aqui reintroduziria o erro de arredondamento que ele existe para
    -- evitar — em dinheiro, `0.1 + 0.2` importa.
    renda_declarada     NUMERIC(15,2) NOT NULL,

    proposta_valor      NUMERIC(15,2) NOT NULL,
    proposta_prazo      INTEGER     NOT NULL,
    -- `(6,2)` e nao `(6,4)`, e a diferenca foi medida.
    --
    -- A primeira versao desta coluna tinha quatro casas, argumentando que 1,9925%
    -- arredondado para 1,99% muda o total de um contrato de 36 meses. O argumento
    -- e valido e a coluna estava errada: `Percentual.__post_init__` quantiza em
    -- **duas** casas, entao o dominio nunca entrega o terceiro digito.
    --
    -- Coluna com mais precisao que o dominio nao e folga — e convite. Alguem
    -- gravaria 1,9925 acreditando que ficou guardado, e o valor voltaria 1,99 sem
    -- nada indicando onde se perdeu. Se o negocio precisar de quatro casas, a
    -- mudanca comeca em `Percentual` e o schema acompanha.
    proposta_taxa       NUMERIC(6,2)  NOT NULL,

    -- Parecer. Todo NULL enquanto a analise nao foi avaliada.
    parecer_decisao         TEXT,
    parecer_nivel_risco     TEXT,
    parecer_score           INTEGER,
    parecer_comprometimento NUMERIC(6,2),
    parecer_limite          NUMERIC(15,2),
    parecer_justificativas  TEXT[] NOT NULL DEFAULT '{}',
    parecer_politicas       TEXT[] NOT NULL DEFAULT '{}',

    criada_em           TIMESTAMPTZ NOT NULL,
    atualizada_em       TIMESTAMPTZ NOT NULL
);

-- Listagem ordena por `criada_em DESC, id DESC`.
--
-- O `id` no desempate nao e enfeite: o relogio do Windows tem resolucao de ~15ms,
-- e duas analises criadas em sequencia recebem o mesmo timestamp. Sem ele a ordem
-- entre elas seria indefinida e a paginacao poderia repetir ou pular registro.
CREATE INDEX IF NOT EXISTS idx_analise_criada ON analise (criada_em DESC, id DESC);

CREATE TABLE IF NOT EXISTS documento (
    id                  UUID PRIMARY KEY,
    -- `ON DELETE CASCADE`: o documento nao existe fora da analise. Sem isso, apagar
    -- uma analise (LGPD art. 18, exclusao a pedido do titular) deixaria orfaos com
    -- hash e referencia do documento — ou seja, rastro de dado pessoal.
    analise_id          UUID        NOT NULL REFERENCES analise(id) ON DELETE CASCADE,

    tipo                TEXT        NOT NULL,
    nome_arquivo        TEXT        NOT NULL,
    conteudo_hash       TEXT        NOT NULL,
    estado              TEXT        NOT NULL,

    texto_extraido      TEXT,
    confianca_ocr       NUMERIC(6,2),
    motor_ocr           TEXT,
    erro                TEXT,

    -- Referencia ao objeto guardado, com **versao**. As duas colunas juntas, e nao
    -- so a chave: e a versao que torna a referencia imutavel, e sem ela a
    -- deduplicacao por versao viraria deduplicacao por chave.
    referencia_chave    TEXT,
    referencia_versao   TEXT,

    injecao_suspeita    BOOLEAN     NOT NULL DEFAULT FALSE,
    categorias_injecao  TEXT[]      NOT NULL DEFAULT '{}',
    exige_revisao       BOOLEAN     NOT NULL DEFAULT FALSE,
    renda_comprovada    NUMERIC(15,2),

    -- De qual campo do holerite a renda saiu: 'liquido' ou 'base'.
    --
    -- `renda_comprovada` responde "quanto" e nao responde "de que", e os dois nao valem o mesmo: o
    -- liquido e o que entra na conta e paga parcela, o bruto e ~20% maior. Um caso aprovado meses
    -- atras precisa poder dizer qual sustentou o parecer, e deduzir depois exigiria reprocessar uma
    -- imagem que pode nao existir mais.
    --
    -- TEXT com CHECK e nao ENUM do Postgres: adicionar valor a um ENUM exige `ALTER TYPE`, que em
    -- versoes anteriores a 12 nao roda dentro de transacao — e o dominio desta coluna pode crescer
    -- (um extrato com renda por mediana, um informe de rendimentos).
    --
    -- NULL e valido e significativo: extrato bancario nao tem a distincao bruto/liquido.
    renda_origem        TEXT        CHECK (renda_origem IN ('liquido', 'base')),

    submetido_em        TIMESTAMPTZ NOT NULL,
    -- Ordem estavel dentro do agregado. Sem ela, `SELECT` sem `ORDER BY` devolve
    -- em ordem arbitraria, e `analise.documentos[0]` num teste passaria ou falharia
    -- conforme o plano de execucao.
    ordem               INTEGER     NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_documento_analise ON documento (analise_id, ordem);

-- Consulta por documento_id, usada pelo `GET /v1/documentos/{id}`.
--
-- Sem ela, a rota varre todas as analises — o que o router faz hoje, com o
-- comentario admitindo a limitacao. Com o indice, e um lookup direto.
CREATE INDEX IF NOT EXISTS idx_documento_id ON documento (id);

CREATE TABLE IF NOT EXISTS dado_extraido (
    id            BIGSERIAL PRIMARY KEY,
    analise_id    UUID        NOT NULL REFERENCES analise(id) ON DELETE CASCADE,
    -- Pode ser NULL: nem todo dado vem de documento (renda declarada, por exemplo).
    -- `ON DELETE SET NULL` e nao CASCADE — apagar o documento nao deve apagar o dado
    -- que sustentou um parecer ja emitido; ele perde a procedencia, nao a existencia.
    documento_id  UUID        REFERENCES documento(id) ON DELETE SET NULL,

    campo         TEXT        NOT NULL,
    valor         TEXT        NOT NULL,
    origem        TEXT        NOT NULL,
    confianca     NUMERIC(6,2) NOT NULL,
    ordem         INTEGER     NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_dado_analise ON dado_extraido (analise_id, ordem);
