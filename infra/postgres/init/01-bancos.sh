#!/bin/bash
# Cria os bancos de desenvolvimento e de teste, aplicando o mesmo schema nos dois.
#
# Bancos separados por um motivo pratico: os testes de integracao dao TRUNCATE
# na tabela de trechos. Apontados para o banco de desenvolvimento, eles apagam
# o corpus ingerido — e o sintoma aparece depois, como "a busca nao retorna
# nada", longe da causa.
#
# `schema.sql` fica fora de /docker-entrypoint-initdb.d de proposito: tudo que
# esta la dentro roda automaticamente, e queremos aplica-lo duas vezes, de
# forma controlada, em vez de uma vez no banco padrao.

set -euo pipefail

BANCO_TESTE="${POSTGRES_DB}_test"

echo "Criando banco de teste: ${BANCO_TESTE}"
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
	CREATE DATABASE ${BANCO_TESTE};
EOSQL

for banco in "$POSTGRES_DB" "$BANCO_TESTE"; do
	echo "Aplicando schema em: ${banco}"
	psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$banco" -f /schema.sql
done

echo "Bancos prontos."
