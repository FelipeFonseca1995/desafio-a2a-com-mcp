import json
import os
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, create_model

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.resolve import Resolve, Elicit
from mcp.server.elicitation import (
    ElicitationResult,
    AcceptedElicitation,
    DeclinedElicitation,
    CancelledElicitation,
)
from mcp.server.request_state import RequestStateSecurity

ROOT_DIR = Path(__file__).resolve().parent.parent
DADOS_DIR = ROOT_DIR / "dados"
SALAS_FILE = DADOS_DIR / "salas.json"
RESERVAS_FILE = DADOS_DIR / "reservas.json"
POLITICA_FILE = DADOS_DIR / "politica-de-uso.md"

# Carregar salas
with open(SALAS_FILE, "r", encoding="utf-8") as f:
    SALAS_DADOS = json.load(f)

SALAS_POR_ID = {s["id"]: s for s in SALAS_DADOS}

# Carregar reservas iniciais
with open(RESERVAS_FILE, "r", encoding="utf-8") as f:
    RESERVAS_INICIAIS = json.load(f)

# Estado em memória das reservas
reservas_em_memoria: list[dict[str, Any]] = [dict(r) for r in RESERVAS_INICIAIS]

# Leitura da política
with open(POLITICA_FILE, "r", encoding="utf-8") as f:
    POLITICA_CONTEUDO = f.read()

# Extrair versão da primeira linha (ex: "versao: 2026-11-01" -> "2026-11-01")
POLITICA_VERSAO = "2026-11-01"
for linha in POLITICA_CONTEUDO.splitlines():
    if linha.startswith("versao:"):
        POLITICA_VERSAO = linha.split("versao:", 1)[1].strip()
        break

# Chave secreta obrigatória para o requestState
REQUEST_STATE_SECRET = os.environ.get("REQUEST_STATE_SECRET")
if not REQUEST_STATE_SECRET or len(REQUEST_STATE_SECRET.encode("utf-8")) < 32:
    raise RuntimeError(
        "A variavel de ambiente REQUEST_STATE_SECRET e obrigatoria e deve ter no minimo 32 bytes de aleatoriedade."
    )

sec = RequestStateSecurity(keys=[REQUEST_STATE_SECRET], ttl=600.0)
mcp_app = MCPServer(
    name="central-de-salas",
    version="1.0.0",
    request_state_security=sec,
)


# Modelos Pydantic para schemas e respostas
class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


def parse_iso(dt_str: str) -> datetime:
    return datetime.fromisoformat(dt_str)


def validar_intervalo(inicio_str: str, fim_str: str) -> tuple[datetime, datetime]:
    try:
        dt_inicio = parse_iso(inicio_str)
        dt_fim = parse_iso(fim_str)
    except Exception:
        raise ToolError("Intervalo invalido: fim deve ser posterior a inicio")

    if dt_fim <= dt_inicio:
        raise ToolError("Intervalo invalido: fim deve ser posterior a inicio")

    t_min = time(8, 0, 0)
    t_max = time(20, 0, 0)
    # A politica permite reservas entre 08:00 e 20:00 (inicio >= 08:00 e <= 20:00, fim >= 08:00 e <= 20:00)
    if not (t_min <= dt_inicio.time() <= t_max) or not (t_min <= dt_fim.time() <= t_max):
        raise ToolError("Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00")

    if (dt_fim - dt_inicio) > timedelta(hours=2):
        raise ToolError("Duracao acima do limite: a politica permite no maximo 2 horas")

    return dt_inicio, dt_fim


def validar_sala_e_intervalo(sala: str, inicio_str: str, fim_str: str) -> tuple[datetime, datetime]:
    if sala not in SALAS_POR_ID:
        raise ToolError(f"Sala inexistente: {sala}")
    return validar_intervalo(inicio_str, fim_str)


def tem_sobreposicao(inicio1: datetime, fim1: datetime, inicio2_str: str, fim2_str: str) -> bool:
    inicio2 = parse_iso(inicio2_str)
    fim2 = parse_iso(fim2_str)
    return inicio1 < fim2 and fim1 > inicio2


def buscar_conflitos(sala: str, dt_inicio: datetime, dt_fim: datetime) -> list[dict[str, Any]]:
    conflitos = []
    for r in reservas_em_memoria:
        if r["sala"] == sala and tem_sobreposicao(dt_inicio, dt_fim, r["inicio"], r["fim"]):
            conflitos.append(r)
    return conflitos


def calcular_alternativas(sala_pedida_id: str, dt_inicio: datetime, dt_fim: datetime) -> list[str]:
    sala_pedida = SALAS_POR_ID[sala_pedida_id]
    cap_minima = sala_pedida["capacidade"]

    alternativas = []
    for s in SALAS_DADOS:
        s_id = s["id"]
        if s_id == sala_pedida_id:
            continue
        if s["capacidade"] < cap_minima:
            continue
        # Verificar se está livre no intervalo
        conflitos = buscar_conflitos(s_id, dt_inicio, dt_fim)
        if not conflitos:
            alternativas.append(s)

    # Ordenar por capacidade crescente e depois por id em ordem alfabética
    alternativas.sort(key=lambda x: (x["capacidade"], x["id"]))
    # No máximo três
    selecionadas = alternativas[:3]
    return [s["id"] for s in selecionadas]


def gerar_proximo_id_reserva() -> str:
    max_id = 0
    for r in reservas_em_memoria:
        rid = r["id"]
        if rid.startswith("res-"):
            try:
                num = int(rid.split("-")[1])
                if num > max_id:
                    max_id = num
            except Exception:
                pass
    return f"res-{max_id + 1:04d}"


# Resource da política
@mcp_app.resource("politica://uso", mime_type="text/markdown")
def politica_resource() -> str:
    return POLITICA_CONTEUDO


# Tool 1: listar_salas
@mcp_app.tool(description="Lista todas as salas com capacidade e recursos.")
def listar_salas() -> ListaDeSalas:
    salas = [SalaOut(**s) for s in SALAS_DADOS]
    return ListaDeSalas(salas=salas)


# Tool 2: consultar_disponibilidade
@mcp_app.tool(description="Diz se uma sala esta livre no intervalo, e quais reservas conflitam.")
def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    dt_inicio, dt_fim = validar_sala_e_intervalo(sala, inicio, fim)
    conflitos_brutos = buscar_conflitos(sala, dt_inicio, dt_fim)
    conflitos = [
        ConflitoOut(
            id=c["id"],
            inicio=c["inicio"],
            fim=c["fim"],
            responsavel=c["responsavel"],
        )
        for c in conflitos_brutos
    ]
    return Disponibilidade(
        sala=sala,
        livre=len(conflitos) == 0,
        conflitos=conflitos,
    )


# Resolver do MRTR para escolha_de_sala
def escolha_de_sala(sala: str, inicio: str, fim: str, responsavel: str) -> Elicit[Any] | None:
    dt_inicio, dt_fim = validar_sala_e_intervalo(sala, inicio, fim)
    conflitos = buscar_conflitos(sala, dt_inicio, dt_fim)
    if not conflitos:
        return None

    alternativas = calcular_alternativas(sala, dt_inicio, dt_fim)
    if not alternativas:
        raise ToolError("Sem alternativas disponiveis no intervalo")

    if len(alternativas) > 1:
        Lit = Literal.__getitem__(tuple(alternativas))
    else:
        Lit = Literal.__getitem__(alternativas[0])

    EscolhaModel = create_model(
        "EscolhaModel",
        sala=(Lit, Field(description="Sala alternativa escolhida", title="Sala")),
    )
    return Elicit("A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.", EscolhaModel)


# Tool 3: reservar_sala
@mcp_app.tool(description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.")
def reservar_sala(
    sala: str,
    inicio: str,
    fim: str,
    responsavel: str,
    escolha: Annotated[ElicitationResult[Any], Resolve(escolha_de_sala)],
) -> ReservaOut:
    if isinstance(escolha, DeclinedElicitation | CancelledElicitation):
        return ReservaOut(
            reserva=None,
            reservado=False,
            sala=None,
            inicio=None,
            fim=None,
            responsavel=None,
            politica=None,
            motivo="recusado",
        )

    # Determinar a sala efetiva
    if escolha.data is not None and hasattr(escolha.data, "sala"):
        sala_efetiva = escolha.data.sala
    else:
        sala_efetiva = sala

    # Criar e salvar reserva
    reserva_id = gerar_proximo_id_reserva()
    nova_reserva = {
        "id": reserva_id,
        "sala": sala_efetiva,
        "inicio": inicio,
        "fim": fim,
        "responsavel": responsavel,
    }
    reservas_em_memoria.append(nova_reserva)

    return ReservaOut(
        reserva=reserva_id,
        reservado=True,
        sala=sala_efetiva,
        inicio=inicio,
        fim=fim,
        responsavel=responsavel,
        politica=POLITICA_VERSAO,
        motivo=None,
    )


# Middleware para registrar todo request no stderr
class StderrLoggingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] == "POST":
            body_bytes = b""
            more_body = True
            messages = []
            while more_body:
                msg = await receive()
                messages.append(msg)
                body_bytes += msg.get("body", b"")
                more_body = msg.get("more_body", False)

            try:
                data = json.loads(body_bytes.decode("utf-8"))
                method = data.get("method", "unknown")
                req_id = data.get("id", "unknown")
                meta = (data.get("params") or {}).get("_meta") or {}
                traceparent = meta.get("traceparent", None)
                sys.stderr.write(f"[MCP] method={method} id={req_id} traceparent={traceparent}\n")
                sys.stderr.flush()
            except Exception:
                pass

            idx = 0

            async def replay_receive():
                nonlocal idx
                if idx < len(messages):
                    m = messages[idx]
                    idx += 1
                    return m
                return await receive()

            await self.app(scope, replay_receive, send)
            return

        await self.app(scope, receive, send)


def create_app():
    starlette_app = mcp_app.streamable_http_app(transport_security=None)
    return StderrLoggingMiddleware(starlette_app)


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", os.environ.get("MCP_PORT", 7301)))
    host = os.environ.get("HOST", os.environ.get("MCP_HOST", "127.0.0.1"))
    app = create_app()
    uvicorn.run(app, host=host, port=port, log_level="warning")
