"""Cliente do servico de KYC, com disjuntor.

## O problema que um cliente HTTP ingenuo cria

Chamar outro servico dentro da esteira de credito adiciona a latencia dele **e** a
disponibilidade dele. Com um cliente simples, um KYC fora do ar significa que toda
analise de credito espera o timeout inteiro antes de desistir — e se o timeout e
3s com duas tentativas, cada requisicao passa a levar 9s.

O efeito nao e "ficar mais lento": e **falha em cascata**. As requisicoes se
acumulam, o pool de conexoes esgota, e o servico de credito cai junto com o de
conformidade. Um servico que depende de outro sem disjuntor nao e mais resiliente
que o mais fragil dos dois — e menos.

O disjuntor troca isso por uma falha rapida e explicita: depois de N falhas
seguidas ele abre e, enquanto estiver aberto, responde `INDISPONIVEL` em
microssegundos, sem tocar na rede. A esteira continua atendendo, e cada caso vai
para revisao humana com o motivo registrado.

## Retry so onde retry ajuda

Duas tentativas, e apenas para erro transitorio: timeout, falha de conexao e 5xx.
Um 422 nao melhora na segunda tentativa — repetir um erro de contrato apenas
multiplica carga num servico que ja respondeu o que tinha a responder.

## Traducao estrita da resposta

Um valor de decisao desconhecido levanta erro em vez de virar um default. Se o
`kyc-compliance` adicionar um estado novo, isto aparece como falha visivel — e o
caso vai para revisao humana pelo caminho de indisponibilidade. Mapear silenciosamente
para "aprovado" seria a pior forma possivel de descobrir a mudanca.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx
import structlog

from credit_analysis.domain.kyc import DecisaoKYC, ResultadoKYC
from credit_analysis.infrastructure.observabilidade import metricas
from credit_analysis.infrastructure.observabilidade.tracing import span

logger = structlog.get_logger(__name__)

# Timeout por tentativa. A triagem do outro servico e comparacao em memoria (ordem
# de milissegundos), entao 3s ja e folga generosa: se passar disso, o problema nao e
# lentidao de calculo, e rede ou saturacao.
TIMEOUT_PADRAO = 3.0

# Tentativas totais, contando a primeira. Duas porque a falha que retry resolve e a
# transitoria (conexao recusada durante um rollout, pacote perdido); a terceira
# tentativa raramente muda o desfecho e triplica o custo do caminho ruim.
TENTATIVAS = 2

# Falhas consecutivas para abrir o disjuntor.
#
# Cinco e alto o suficiente para nao abrir por causa de um pod reiniciando durante
# um rolling update, e baixo o suficiente para nao deixar dezenas de requisicoes
# pagarem timeout completo antes de o circuito proteger.
FALHAS_PARA_ABRIR = 5

# Quanto o circuito fica aberto antes de testar de novo. Trinta segundos cobre um
# restart de pod; menos que isso faria o circuito oscilar durante o proprio rollout.
ESPERA_PARA_TESTAR = 30.0


class EstadoDisjuntor:
    FECHADO = "fechado"
    ABERTO = "aberto"
    MEIO_ABERTO = "meio_aberto"


@dataclass
class Disjuntor:
    """Disjuntor de tres estados, implementado a mao.

    Sao ~40 linhas contra uma dependencia nova (`pybreaker`, `purgatory`) que
    traria configuracao, decorador e um modelo mental a mais. Para um unico ponto
    de integracao, o codigo direto e mais facil de auditar do que a biblioteca — e
    o comportamento fica explicito no lugar em que alguem vai procurar.

    Estados:
      FECHADO      passa tudo, contando falhas consecutivas
      ABERTO       recusa sem tocar na rede, ate a janela expirar
      MEIO_ABERTO  deixa UMA requisicao passar; se ela vencer, fecha; se falhar,
                   abre de novo. E o que evita reabrir a torneira inteira num
                   servico que ainda esta se recuperando.
    """

    falhas_para_abrir: int = FALHAS_PARA_ABRIR
    espera_para_testar: float = ESPERA_PARA_TESTAR

    _falhas: int = 0
    _aberto_em: float | None = None
    _testando: bool = False

    @property
    def estado(self) -> str:
        if self._aberto_em is None:
            return EstadoDisjuntor.FECHADO
        if self._testando:
            return EstadoDisjuntor.MEIO_ABERTO
        return EstadoDisjuntor.ABERTO

    def permite(self) -> bool:
        """Se a proxima chamada pode ir para a rede."""
        if self._aberto_em is None:
            return True

        if time.monotonic() - self._aberto_em >= self.espera_para_testar:
            # Uma unica tentativa de sondagem.
            self._testando = True
            return True

        return False

    def registrar_sucesso(self) -> None:
        if self._aberto_em is not None:
            logger.info("kyc.disjuntor_fechou", falhas_anteriores=self._falhas)
        self._falhas = 0
        self._aberto_em = None
        self._testando = False

    def registrar_falha(self) -> None:
        self._falhas += 1
        self._testando = False

        if self._falhas >= self.falhas_para_abrir and self._aberto_em is None:
            self._aberto_em = time.monotonic()
            # Warning e nao info: circuito aberto significa que toda analise passa a
            # exigir revisao humana. E um evento operacional, nao um detalhe.
            logger.warning(
                "kyc.disjuntor_abriu",
                falhas=self._falhas,
                segundos_ate_testar=self.espera_para_testar,
            )
        elif self._aberto_em is not None:
            # Falhou a sondagem do meio-aberto: reinicia a janela.
            self._aberto_em = time.monotonic()


class ClienteKYCHttp:
    """Adapter do port `ConsultaKYC` sobre o `kyc-compliance`."""

    def __init__(
        self,
        url_base: str,
        timeout_segundos: float = TIMEOUT_PADRAO,
        tentativas: int = TENTATIVAS,
        disjuntor: Disjuntor | None = None,
    ) -> None:
        self._url = url_base.rstrip("/")
        self._timeout = timeout_segundos
        self._tentativas = max(1, tentativas)
        self._disjuntor = disjuntor or Disjuntor()
        self._cliente: httpx.AsyncClient | None = None

    @property
    def identificacao(self) -> str:
        return f"http:{self._url}"

    @property
    def estado_disjuntor(self) -> str:
        return self._disjuntor.estado

    async def fechar(self) -> None:
        if self._cliente is not None:
            await self._cliente.aclose()
            self._cliente = None

    def _obter_cliente(self) -> httpx.AsyncClient:
        # Cliente reaproveitado: criar um por requisicao descartaria o pool de
        # conexoes e pagaria handshake TCP a cada analise.
        if self._cliente is None:
            self._cliente = httpx.AsyncClient(
                base_url=self._url,
                timeout=self._timeout,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
        return self._cliente

    async def triar(self, nome: str, cpf: str) -> ResultadoKYC:
        """Consulta a triagem. **Nunca levanta excecao de rede.**

        Devolver `INDISPONIVEL` em vez de propagar o erro e o que permite ao dominio
        decidir o que fazer com a ausencia de informacao (ver `domain/kyc.py`). Se
        este metodo estourasse, o caso de uso marcaria a analise como FALHA — e uma
        analise de credito nao falhou porque o KYC caiu; ela ficou incompleta, o que
        e diferente e tem tratamento proprio.
        """
        if not self._disjuntor.permite():
            metricas.kyc_consultas.labels(resultado="circuito_aberto").inc()
            logger.warning("kyc.recusado_pelo_disjuntor", estado=self._disjuntor.estado)
            return ResultadoKYC.nao_consultado(
                "circuito aberto apos falhas consecutivas do servico de conformidade"
            )

        inicio = time.perf_counter()
        ultimo_erro = "erro desconhecido"

        for tentativa in range(1, self._tentativas + 1):
            try:
                with span(
                    "kyc.triar",
                    **{"kyc.tentativa": tentativa, "kyc.url": self._url},
                ):
                    resultado = await self._chamar(nome, cpf)
            except _ErroTransitorio as exc:
                ultimo_erro = str(exc)
                if tentativa < self._tentativas:
                    # Espera curta e fixa. Backoff exponencial faria sentido com
                    # mais tentativas; com duas, ele so adiaria a mesma decisao.
                    await asyncio.sleep(0.2)
                    continue
            except _ErroPermanente as exc:
                # Nao ha o que retentar: contrato divergente ou resposta invalida.
                self._disjuntor.registrar_falha()
                metricas.kyc_consultas.labels(resultado="erro_permanente").inc()
                logger.error("kyc.erro_permanente", erro=str(exc))
                return ResultadoKYC.nao_consultado(str(exc))
            else:
                duracao = time.perf_counter() - inicio
                self._disjuntor.registrar_sucesso()
                metricas.kyc_duracao.observe(duracao)
                metricas.kyc_consultas.labels(resultado=resultado.decisao.value).inc()
                logger.info(
                    "kyc.consultado",
                    decisao=resultado.decisao.value,
                    triagem_id=resultado.triagem_id,
                    tentativas=tentativa,
                    duracao_ms=int(duracao * 1000),
                )
                return resultado

        self._disjuntor.registrar_falha()
        metricas.kyc_consultas.labels(resultado="indisponivel").inc()
        logger.warning("kyc.indisponivel", erro=ultimo_erro, tentativas=self._tentativas)
        return ResultadoKYC.nao_consultado(ultimo_erro)

    async def _chamar(self, nome: str, cpf: str) -> ResultadoKYC:
        cliente = self._obter_cliente()

        try:
            resposta = await cliente.post(
                "/v1/triagens",
                json={"nome": nome, "cpf": cpf},
                headers=_cabecalhos_de_correlacao(),
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise _ErroTransitorio(f"{type(exc).__name__} ao consultar o KYC") from exc

        if resposta.status_code >= 500:
            raise _ErroTransitorio(f"KYC respondeu {resposta.status_code}")

        if resposta.status_code >= 400:
            # 4xx e divergencia de contrato ou dado invalido: retentar nao ajuda.
            raise _ErroPermanente(f"KYC recusou a consulta com {resposta.status_code}")

        return _traduzir(resposta.json())


class KYCFake:
    """Duplo deterministico do port.

    Diferente do `LLMFake`, este **nao** e uma degradacao de producao: sem KYC
    configurado o gate simplesmente nao e aplicado, e em producao a ausencia de
    configuracao impede a subida. Aqui ele existe para os testes exercitarem os
    quatro desfechos, inclusive a indisponibilidade — que e o dificil de provocar
    com um servico real de proposito.
    """

    def __init__(self, resultado: ResultadoKYC | None = None) -> None:
        self._resultado = resultado or ResultadoKYC(
            decisao=DecisaoKYC.APROVADO,
            justificativas=("Nenhuma correspondencia em lista restritiva",),
            triagem_id="fake",
        )
        self.consultas: list[tuple[str, str]] = []

    @property
    def identificacao(self) -> str:
        return "fake"

    async def triar(self, nome: str, cpf: str) -> ResultadoKYC:
        self.consultas.append((nome, cpf))
        return self._resultado


class _ErroTransitorio(Exception):
    """Vale retentar."""


class _ErroPermanente(Exception):
    """Nao vale retentar."""


def _traduzir(corpo: object) -> ResultadoKYC:
    """Converte o JSON do outro servico em tipo de dominio.

    Estrito de proposito: chave ausente ou decisao desconhecida viram
    `_ErroPermanente`. O custo de ser estrito e um erro visivel quando o contrato
    muda; o custo de ser tolerante e uma analise aprovada com base num campo que o
    outro servico parou de enviar.
    """
    if not isinstance(corpo, dict):
        raise _ErroPermanente("resposta do KYC nao e um objeto JSON")

    bruto = corpo.get("decisao")
    if not isinstance(bruto, str):
        raise _ErroPermanente("resposta do KYC sem o campo 'decisao'")

    try:
        decisao = DecisaoKYC(bruto)
    except ValueError as exc:
        raise _ErroPermanente(
            f"decisao desconhecida do KYC: '{bruto}'. O contrato do servico de "
            f"conformidade mudou e a traducao precisa ser atualizada."
        ) from exc

    if decisao is DecisaoKYC.INDISPONIVEL:
        # `INDISPONIVEL` e estado local desta esteira; o outro servico nunca o envia.
        # Receber isso significa contrato divergente.
        raise _ErroPermanente("KYC devolveu um estado que so existe localmente")

    justificativas = corpo.get("justificativas")
    return ResultadoKYC(
        decisao=decisao,
        nivel_risco=str(corpo.get("nivel_risco") or ""),
        justificativas=tuple(str(j) for j in justificativas)
        if isinstance(justificativas, list)
        else (),
        triagem_id=str(corpo["id"]) if corpo.get("id") else None,
    )


def _cabecalhos_de_correlacao() -> dict[str, str]:
    """Propaga o request id para o outro servico.

    E o que faz os dois servicos logarem o mesmo identificador — sem isso,
    investigar uma analise que consultou o KYC exige cruzar timestamp entre dois
    conjuntos de log. O `kyc-compliance` reaproveita o cabecalho em vez de gerar um
    novo, justamente para fechar essa corrente.
    """
    contexto = structlog.contextvars.get_contextvars()
    request_id = contexto.get("request_id")
    return {"X-Request-ID": str(request_id)} if request_id else {}
