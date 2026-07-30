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
        conferir_deployment(nome, deployment, erros)

    for policy in (obj for obj in objetos if obj["kind"] == "NetworkPolicy"):
        conferir_policy(nome, policy, erros)


def conferir_deployment(nome: str, deployment: dict[str, Any], erros: list[str]) -> None:
    spec = deployment["spec"]["template"]["spec"]
    contexto_pod = spec.get("securityContext", {})

    if not contexto_pod.get("runAsNonRoot"):
        erros.append(f"{nome}: pod nao declara runAsNonRoot")

    for container in spec["containers"]:
        rotulo = f"{nome}/{container['name']}"
        contexto = container.get("securityContext", {})

        # As tres sondas tem papeis distintos, e faltar qualquer uma tem sintoma proprio:
        # sem readiness o pod recebe trafego antes de estar pronto; sem liveness um
        # processo travado fica no balanceador para sempre; sem startup, o liveness
        # precisa ser permissivo o tempo todo para tolerar o boot.
        for sonda in ("readinessProbe", "livenessProbe", "startupProbe"):
            if sonda not in container:
                erros.append(f"{rotulo}: falta {sonda}")

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
