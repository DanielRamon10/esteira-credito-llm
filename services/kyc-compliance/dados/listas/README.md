# Listas restritivas (sinteticas)

**Nenhum nome nestes arquivos e de pessoa real.** Listas de PEP e de sancoes sao
dado publico, mas publicar um recorte delas num repositorio de portfolio
associaria nomes reais a rotulos de restricao fora de contexto — o oposto do
cuidado que um servico de conformidade deveria ter.

Os nomes foram construidos a partir de combinacoes comuns em portugues, escolhidos
para exercitar os casos difíceis do algoritmo de casamento: abreviacao geracional,
acento, inicial do meio, homonimo parcial e nome reordenado.

## Formato

| coluna | obrigatoria | conteudo |
|---|---|---|
| `nome` | sim | como consta na lista, normalmente em caixa alta |
| `tipo` | sim | `pep`, `sancao` ou `midia_negativa` |
| `origem` | nao | fonte declarada; sem valor, usa o nome do arquivo |
| `cpf` | nao | quando a fonte traz; CPF identico domina a decisao |
| `cargo` | nao | relevante para PEP |
| `observacao` | nao | contexto para o analista |

Linha com `tipo` invalido **derruba o carregamento**. Ignorar em silencio
significaria uma pessoa sancionada fora da triagem por causa de um erro de
digitacao no arquivo.
