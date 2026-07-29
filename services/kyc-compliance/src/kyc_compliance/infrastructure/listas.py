"""Adapters do port `RepositorioListas`.

## As listas deste repositorio sao sinteticas, e isso precisa estar dito

Nenhum nome aqui e de pessoa real. Listas de PEP e de sancoes sao dado publico,
mas publicar um recorte delas num repositorio de portfolio associaria nomes reais
a rotulos de restricao fora de contexto — o que e o oposto do cuidado que um
servico de conformidade deveria ter. Os nomes foram inventados a partir de
combinacoes comuns em portugues, escolhidos para exercitar os casos difíceis do
algoritmo: abreviacao, acento, homonimo e nome parcial.

## Carregamento no boot, nao por requisicao

A lista inteira vai para memoria na subida. Sao alguns milhares de entradas na
pratica (a de sancoes da ONU tem ~700 pessoas fisicas), o que cabe folgadamente, e
a alternativa — consultar arquivo ou banco a cada triagem — pagaria I/O para
comparar contra o mesmo conteudo.

O preco e que atualizar a lista exige reiniciar o pod, e isso e aceitavel porque
lista oficial muda em ciclo de dias. Se passasse a mudar em minutos, o desenho
correto seria recarga periodica com versao — e o `procedencia` existe justamente
para que a versao em uso apareca na trilha de auditoria.
"""

from __future__ import annotations

import csv
from pathlib import Path

import structlog

from kyc_compliance.domain.triagem import EntradaRestritiva, TipoLista

logger = structlog.get_logger(__name__)


class ListasEmMemoria:
    """Entradas fornecidas direto no construtor — usado em teste."""

    def __init__(self, entradas: list[EntradaRestritiva], procedencia: str = "memoria") -> None:
        self._entradas = list(entradas)
        self._procedencia = procedencia

    def todas(self) -> list[EntradaRestritiva]:
        # Copia rasa a cada chamada: o dominio recebe a lista e nao deve poder
        # alterar a fonte. Entrada e frozen, entao a copia rasa basta.
        return list(self._entradas)

    @property
    def total(self) -> int:
        return len(self._entradas)

    @property
    def procedencia(self) -> str:
        return self._procedencia


class ListasDeArquivo:
    """Le CSV do disco, uma vez, na construcao.

    Falha alto quando o diretorio nao existe ou esta vazio. Um servico de
    conformidade que sobe com lista vazia aprova todo mundo e reporta "nenhuma
    correspondencia" — degradacao silenciosa na direcao mais perigosa possivel.
    Melhor nao subir.
    """

    def __init__(self, diretorio: Path) -> None:
        self._diretorio = diretorio
        self._entradas: list[EntradaRestritiva] = []
        self._arquivos: list[str] = []

        if not diretorio.is_dir():
            raise RuntimeError(
                f"Diretorio de listas nao encontrado: {diretorio}. "
                "O servico nao sobe sem lista: uma lista vazia aprovaria todos os "
                "clientes e reportaria 'nenhuma correspondencia'."
            )

        for caminho in sorted(diretorio.glob("*.csv")):
            self._entradas.extend(self._ler(caminho))
            self._arquivos.append(caminho.name)

        if not self._entradas:
            raise RuntimeError(
                f"Nenhuma entrada carregada de {diretorio}. Confira o formato dos CSV "
                "(colunas: nome, tipo, origem, cpf, cargo, observacao)."
            )

        logger.info(
            "kyc.listas_carregadas",
            entradas=len(self._entradas),
            arquivos=self._arquivos,
            por_tipo={t.value: sum(1 for e in self._entradas if e.tipo is t) for t in TipoLista},
        )

    @staticmethod
    def _ler(caminho: Path) -> list[EntradaRestritiva]:
        entradas: list[EntradaRestritiva] = []

        with caminho.open(encoding="utf-8", newline="") as arquivo:
            for numero, linha in enumerate(csv.DictReader(arquivo), start=2):
                nome = (linha.get("nome") or "").strip()
                bruto_tipo = (linha.get("tipo") or "").strip().lower()
                if not nome:
                    continue

                try:
                    tipo = TipoLista(bruto_tipo)
                except ValueError as exc:
                    # Linha invalida derruba o carregamento em vez de ser ignorada.
                    # Pular em silencio significaria uma pessoa sancionada fora da
                    # triagem por causa de um erro de digitacao no arquivo.
                    raise RuntimeError(
                        f"{caminho.name} linha {numero}: tipo '{bruto_tipo}' invalido. "
                        f"Use um de: {[t.value for t in TipoLista]}"
                    ) from exc

                entradas.append(
                    EntradaRestritiva(
                        nome=nome,
                        tipo=tipo,
                        origem=(linha.get("origem") or caminho.stem).strip(),
                        cpf=(linha.get("cpf") or "").strip() or None,
                        cargo=(linha.get("cargo") or "").strip() or None,
                        observacao=(linha.get("observacao") or "").strip() or None,
                    )
                )

        return entradas

    def todas(self) -> list[EntradaRestritiva]:
        return list(self._entradas)

    @property
    def total(self) -> int:
        return len(self._entradas)

    @property
    def procedencia(self) -> str:
        return f"{self._diretorio.name}:{','.join(self._arquivos)}"
