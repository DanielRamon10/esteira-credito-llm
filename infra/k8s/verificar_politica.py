"""Verifica invariantes de politica nos manifests renderizados.

## Por que este arquivo existe

`kubeconform --strict` valida **schema**: um Deployment sem readiness, sem request de
memoria ou rodando como root passa sem uma reclamacao. Schema diz se o YAML e um
Deployment valido, nao se e um Deployment que alguem deveria aplicar.

A base cresceu de um servico para tres, e a forma como um quarto vai nascer e copiando um
diretorio existente. Copia perde detalhe, e os detalhes perdidos aqui sao justamente os
que nao quebram nada no dia do deploy: falta de PDB aparece num drain de no meses depois,
falta de NetworkPolicy aparece quando alguem procura caminho de exfiltracao.

## Por que Python e nao `grep` no workflow

A primeira versao desta checagem era `grep -A 3 "limits:" | grep "cpu:"`, para garantir
que ninguem adicionasse limite de CPU. Ela **acusou os tres servicos** de terem limite de
CPU, e estava errada: `kustomize build` remove comentarios, entao no YAML renderizado o
bloco fica

    limits:
      memory: 3Gi
    requests:
      cpu: 250m

e a janela de 3 linhas do `grep -A` atravessa a fronteira do bloco. Uma checagem de
politica que da falso positivo e pior que nenhuma: o time desliga a checagem, nao o
defeito.

Aqui a estrutura e navegada, nao adivinhada por proximidade de texto.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

RAIZ = Path(__file__).resolve().parents[2]
BASE = Path(__file__).resolve().parent / "base"

SERVICOS = ("credit-analysis", "kyc-compliance", "customer-support")

# Objetos que cada servico precisa ter por conta propria.
#
# Nenhum deles impede o servico de funcionar, e e por isso que estao numa lista
# verificada por maquina: ausencia silenciosa e o modo de falha de todos os tres.
OBJETOS_EXIGIDOS = ("Service", "Deployment", "PodDisruptionBudget", "NetworkPolicy")


def renderizar(caminho: Path) -> list[dict[str, Any]]:
    saida = subprocess.run(  # noqa: S603
        ["kubectl", "kustomize", str(caminho)],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
        cwd=RAIZ,
    ).stdout
    return [doc for doc in yaml.safe_load_all(saida) if doc]


def conferir_servico(nome: str, erros: list[str]) -> None:
    objetos = renderizar(BASE / nome)
    tipos = {obj["kind"] for obj in objetos}

    for exigido in OBJETOS_EXIGIDOS:
        if exigido not in tipos:
            erros.append(f"{nome}: falta {exigido}")

    for deployment in (obj for obj in objetos if obj["kind"] == "Deployment"):
        conferir_pod(nome, deployment["spec"]["template"], erros, exige_sonda=True)

    # CronJob tambem, e a ausencia disto era uma lacuna: o `purga.yaml` da Camada 10 entrou com
    # `securityContext`, recursos e rootfs somente leitura corretos, e **nada** conferia que
    # continuassem assim. Um `kind` fora da varredura e um manifest sem politica.
    for cronjob in (obj for obj in objetos if obj["kind"] == "CronJob"):
        conferir_cronjob(nome, cronjob, erros)

    for policy in (obj for obj in objetos if obj["kind"] == "NetworkPolicy"):
        conferir_policy(nome, policy, erros)


# Nomes de porta que **nao** servem trafego.
#
# `metrics` esta aqui porque um endpoint de metricas nao e uma API: ele expoe contador para o
# Prometheus e nao atende cliente. Um container cuja unica porta e esta nao tem endpoint de trafego
# para uma sonda medir.
#
# A lista e curta de proposito. Cada nome adicionado aqui e uma porta que deixa de exigir sonda, e
# a pergunta para incluir um quarto nao e "esta porta e auxiliar?" e sim "existe cliente cuja
# requisicao depende de este container estar Ready?".
PORTAS_SEM_TRAFEGO = frozenset({"metrics"})


def e_consumidor_de_fila(container: dict[str, Any], rotulos_do_pod: dict[str, str]) -> bool:
    """Se este container e um consumidor de fila, para o qual sonda HTTP nao faz sentido.

    ## Duas condicoes, e nao uma

    - **nenhuma porta de trafego**: o sinal estrutural. Sem endpoint que atenda cliente, nao ha o
      que sondar. Este e o que nao da para satisfazer por engano — no dia em que o processo ganhar
      um servidor, quem escrever `name: http` traz a exigencia de sonda de volta junto;
    - **`component: worker`**: o sinal declarado, e ele existe porque o primeiro sozinho seria
      frouxo demais. Esquecer o bloco `ports` num container de API e um erro plausivel, e com uma
      condicao so ele **removeria** a exigencia de sonda em vez de acusar — exatamente o tipo de
      isencao silenciosa que este arquivo existe para nao ter.

    Precisar dos dois significa que a isencao e uma decisao escrita em dois lugares, e nao um
    efeito colateral de uma omissao.

    ## Por que nao e simplesmente "sem `ports`"

    Era, na primeira versao — e o trabalhador nao declarava porta nenhuma. Ele passou a declarar
    `metrics: 8001` quando ficou medido que, sem endpoint de metricas, a serie
    `credito_extracoes_total` desaparecia do Prometheus e levava consigo a capacidade de o alerta
    `DocumentosPresosNaExtracao` disparar.

    Endurecer a regra para "sem porta alguma" obrigaria a escolher entre a isencao e o alerta. O que
    a regra precisa distinguir nao e ter porta, e sim **servir trafego**.
    """
    nomes = {porta.get("name") for porta in container.get("ports", [])}
    sem_trafego = nomes <= PORTAS_SEM_TRAFEGO
    declarado = rotulos_do_pod.get("app.kubernetes.io/component") == "worker"
    return sem_trafego and declarado


def conferir_porta_das_sondas(
    rotulo: str, container: dict[str, Any], erros: list[str]
) -> None:
    """Sonda por nome de porta precisa apontar para uma porta que existe.

    Esta checagem nasceu de um mutante, e vale registrar como: ao testar a isencao de sondas do
    trabalhador, removi o bloco `ports` do container da API para conferir que a isencao nao era
    frouxa. Ela nao era — a exigencia de sonda continuou valendo. Mas o verificador aceitou o
    manifest **sem uma reclamacao**, e as tres sondas da API dizem `port: http`.

    O resultado seria um pod que nunca fica Ready: o kubelet nao resolve `http`, a readiness falha
    para sempre, e o rollout trava com uma mensagem sobre sonda — nao sobre porta ausente.
    `kubeconform --strict` nao pega: o schema de `httpGet.port` aceita string, e nada no schema
    liga a string ao bloco `ports` do mesmo container.
    """
    declaradas = {porta.get("name") for porta in container.get("ports", [])}

    for sonda in ("readinessProbe", "livenessProbe", "startupProbe"):
        alvo = container.get(sonda, {}).get("httpGet", {}).get("port")
        # Numero e endereco absoluto e nao precisa existir no bloco `ports`; o kubelet conecta na
        # porta do pod direto. Somente a forma por nome depende da declaracao.
        if isinstance(alvo, str) and alvo not in declaradas:
            erros.append(
                f"{rotulo}: {sonda} aponta para a porta '{alvo}', que o container nao declara"
            )


def conferir_cronjob(nome: str, cronjob: dict[str, Any], erros: list[str]) -> None:
    """Politica do pod de um CronJob, mais o que so CronJob tem.

    ## Sonda nao se aplica, e por um motivo diferente do trabalhador

    O trabalhador e isento porque nao serve trafego. Um job e isento porque **termina**: nao existe
    "pronto para receber" num processo que executa e sai, e `livenessProbe` num container de job
    reiniciaria trabalho concluido.

    A isencao aqui e por `kind`, e nao por rotulo — nao ha como um CronJob ser um servidor HTTP por
    engano.
    """
    modelo = cronjob["spec"]["jobTemplate"]["spec"]["template"]
    conferir_pod(nome, modelo, erros, exige_sonda=False)

    spec_job = cronjob["spec"]["jobTemplate"]["spec"]

    # Job sem teto de tempo fica preso ate alguem notar. Com `concurrencyPolicy: Forbid`, as
    # execucoes seguintes sao **puladas em silencio** enquanto isso — o pior par possivel.
    if "activeDeadlineSeconds" not in spec_job:
        erros.append(f"{nome}/cronjob: sem activeDeadlineSeconds; um job travado nunca termina")

    # `Allow` (o default) deixa duas purgas concorrerem pelas mesmas linhas.
    if cronjob["spec"].get("concurrencyPolicy") != "Forbid":
        erros.append(f"{nome}/cronjob: concurrencyPolicy deveria ser Forbid")

    # `restartPolicy` do pod de job so aceita Never ou OnFailure; `Never` com `backoffLimit` deixa a
    # decisao de retentar com o controlador, que a registra em vez de reiniciar o mesmo container.
    if modelo["spec"].get("restartPolicy") != "Never":
        erros.append(f"{nome}/cronjob: restartPolicy deveria ser Never")


def conferir_pod(
    nome: str, modelo: dict[str, Any], erros: list[str], *, exige_sonda: bool
) -> None:
    """Politica comum a qualquer pod, venha ele de Deployment ou de CronJob.

    Era `conferir_deployment` e passou a receber o **modelo de pod**: as regras de securityContext,
    recursos e limite de CPU nao tem nada de especifico a Deployment, e mante-las la deixaria o
    CronJob de fora — que foi exatamente o que aconteceu ate a Camada 10.
    """
    spec = modelo["spec"]
    contexto_pod = spec.get("securityContext", {})

    if not contexto_pod.get("runAsNonRoot"):
        erros.append(f"{nome}: pod nao declara runAsNonRoot")

    rotulos_do_pod = modelo["metadata"].get("labels", {})

    for container in spec["containers"]:
        rotulo = f"{nome}/{container['name']}"
        contexto = container.get("securityContext", {})

        # As tres sondas tem papeis distintos, e faltar qualquer uma tem sintoma proprio:
        # sem readiness o pod recebe trafego antes de estar pronto; sem liveness um
        # processo travado fica no balanceador para sempre; sem startup, o liveness
        # precisa ser permissivo o tempo todo para tolerar o boot.
        #
        # A excecao e consumidor de fila, e ela nao e uma frouxidao: as tres sondas medem
        # disponibilidade **de endpoint**, e num processo sem servidor HTTP nao existe endpoint
        # para medir. Um `livenessProbe` respondendo 200 num trabalhador travado seria pior que
        # nenhum — significaria "saudavel" enquanto a fila cresce. O sinal certo e profundidade de
        # fila (`DocumentosPresosNaExtracao`), e ele vive nos alertas, nao no manifest.
        if not exige_sonda or e_consumidor_de_fila(container, rotulos_do_pod):
            # Impresso e nao silencioso: uma isencao que nao aparece no log do CI e uma isencao
            # que ninguem revisa. Se um dia isto imprimir o nome de um servico que **deveria**
            # ter sonda, a linha e a pista.
            motivo = "consumidor de fila" if exige_sonda else "processo que termina"
            print(f"::notice::{rotulo}: isento de sondas HTTP ({motivo})")
            for sonda in ("readinessProbe", "livenessProbe", "startupProbe"):
                # E o inverso tambem e violacao: declarar sonda HTTP sem porta e configuracao que
                # nunca pode passar, e o pod entraria em CrashLoop por falha de sonda.
                if sonda in container:
                    erros.append(f"{rotulo}: {sonda} num container que nao serve trafego")
        else:
            for sonda in ("readinessProbe", "livenessProbe", "startupProbe"):
                if sonda not in container:
                    erros.append(f"{rotulo}: falta {sonda}")
            conferir_porta_das_sondas(rotulo, container, erros)

        if not contexto.get("readOnlyRootFilesystem"):
            erros.append(f"{rotulo}: rootfs nao e somente leitura")
        if contexto.get("allowPrivilegeEscalation") is not False:
            erros.append(f"{rotulo}: allowPrivilegeEscalation nao esta em false")
        if contexto.get("capabilities", {}).get("drop") != ["ALL"]:
            erros.append(f"{rotulo}: nao descarta todas as capabilities")

        recursos = container.get("resources", {})
        pedidos = recursos.get("requests", {})
        limites = recursos.get("limits", {})

        # Request de memoria sem limite deixa o pod estourar o no; limite sem request faz
        # o scheduler empacotar pods que depois morrem por OOM.
        for chave, mapa, onde in (
            ("memory", pedidos, "requests"),
            ("cpu", pedidos, "requests"),
            ("memory", limites, "limits"),
        ):
            if chave not in mapa:
                erros.append(f"{rotulo}: falta {onde}.{chave}")

        # E a inversa: limite de CPU e proibido por decisao medida. Limite provoca
        # throttling do CFS exatamente no burst de inferencia, que e onde a latencia ja e
        # o problema (80s medidos). O request garante a fatia sob contencao; o limite so
        # puniria o pod quando ha CPU sobrando.
        #
        # Esta e a checagem que a versao com `grep` errava.
        if "cpu" in limites:
            erros.append(
                f"{rotulo}: limite de CPU ({limites['cpu']}) — ver a nota no deployment"
            )

        if container.get("imagePullPolicy") == "Always":
            erros.append(f"{rotulo}: imagePullPolicy Always com tag imutavel e pull inutil")


def conferir_policy(nome: str, policy: dict[str, Any], erros: list[str]) -> None:
    spec = policy["spec"]
    tipos = set(spec.get("policyTypes", []))

    # Policy so de Ingress e a meia-defesa mais comum: bloqueia quem entra e deixa o
    # egress aberto, que e o caminho de exfiltracao.
    for tipo in ("Ingress", "Egress"):
        if tipo not in tipos:
            erros.append(f"{nome}: NetworkPolicy sem policyTypes {tipo}")

    # DNS no egress. Esquecer esta regra e o erro classico: tudo passa a falhar com erro
    # de resolucao de nome, e a policy raramente e a primeira suspeita.
    portas_de_egress = {
        porta.get("port")
        for regra in spec.get("egress", [])
        for porta in regra.get("ports", [])
    }
    if 53 not in portas_de_egress:
        erros.append(f"{nome}: NetworkPolicy sem egress de DNS (porta 53)")


def conferir_fronteira_de_divulgacao(erros: list[str]) -> None:
    """O `customer-support` nao pode ter rota de rede para os servicos internos.

    Esta e a quarta defesa da fronteira de divulgacao, e a unica que nao depende de o
    codigo estar correto. As outras tres — filtro de visibilidade na entrada, guard na
    saida, roteamento deterministico fora do prompt — sao de aplicacao, e portanto
    quebraveis por refatoracao. A ausencia de rota nao e.
    """
    objetos = renderizar(BASE / "customer-support")
    (policy,) = (obj for obj in objetos if obj["kind"] == "NetworkPolicy")

    proibidos = {"credit-analysis", "kyc-compliance"}
    for regra in policy["spec"].get("egress", []):
        for destino in regra.get("to", []):
            rotulos = destino.get("podSelector", {}).get("matchLabels", {})
            alvo = rotulos.get("app.kubernetes.io/name")
            if alvo in proibidos:
                erros.append(
                    f"customer-support: egress para {alvo} quebra a fronteira de divulgacao"
                )


def main() -> int:
    erros: list[str] = []

    for nome in SERVICOS:
        conferir_servico(nome, erros)
    conferir_fronteira_de_divulgacao(erros)

    # A base agregada precisa construir, e precisa conter os tres. Validar apenas os
    # componentes deixaria passar um `resources:` esquecido no agregador.
    tipos_na_base = [obj["metadata"]["name"] for obj in renderizar(BASE)]
    for nome in SERVICOS:
        if nome not in tipos_na_base:
            erros.append(f"base agregada nao inclui {nome}")

    if erros:
        for erro in erros:
            print(f"::error::{erro}")
        print(f"\n{len(erros)} violacoes de politica")
        return 1

    print(f"politica ok: {len(SERVICOS)} servicos verificados")
    return 0


if __name__ == "__main__":
    sys.exit(main())
