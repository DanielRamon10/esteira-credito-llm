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
