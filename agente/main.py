import json
import os
import re
import secrets
import urllib.error
import urllib.request
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route


MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:7301/mcp")
AGENTE_HOST = os.environ.get("HOST", os.environ.get("AGENTE_HOST", "127.0.0.1"))
AGENTE_PORT = int(os.environ.get("PORT", os.environ.get("AGENTE_PORT", 7300)))


# ============================================================================
# Cliente MCP (Host MCP por dentro)
# ============================================================================
class McpClient:
    def __init__(self, url: str):
        self.url = url
        self.tools: list[dict[str, Any]] = []
        self.politica_versao: str = "2026-11-01"
        self._descoberto: bool = False

    def descobrir(self, traceparent: str | None = None) -> None:
        if self._descoberto:
            return

        # 1. tools/list para descobrir ferramentas em runtime
        resp_tools = self._post("tools/list", {}, nome=None, traceparent=traceparent)
        self.tools = (resp_tools.get("result") or {}).get("tools", [])

        # 2. resources/read de politica://uso
        resp_pol = self._post(
            "resources/read",
            {"uri": "politica://uso"},
            nome="politica://uso",
            traceparent=traceparent,
        )
        contents = ((resp_pol.get("result") or {}).get("contents") or [{}])[0].get("text", "")
        for linha in contents.splitlines():
            if linha.startswith("versao:"):
                self.politica_versao = linha.split("versao:", 1)[1].strip()
                break

        self._descoberto = True

    def _post(
        self,
        metodo: str,
        params: dict[str, Any],
        nome: str | None = None,
        traceparent: str | None = None,
    ) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientInfo": {
                "name": "agente-central-de-salas",
                "version": "1.0.0",
            },
            "io.modelcontextprotocol/clientCapabilities": {
                "elicitation": {
                    "form": {},
                }
            },
        }
        if traceparent:
            meta["traceparent"] = traceparent

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2026-07-28",
            "Mcp-Method": metodo,
        }
        if nome:
            headers["Mcp-Name"] = nome

        corpo = {
            "jsonrpc": "2.0",
            "id": secrets.token_hex(6),
            "method": metodo,
            "params": {**params, "_meta": meta},
        }

        data_bytes = json.dumps(corpo).encode("utf-8")
        req = urllib.request.Request(self.url, data=data_bytes, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return json.loads(e.read().decode("utf-8"))

    def chamar_reservar_sala(self, arguments: dict[str, Any], traceparent: str | None = None) -> dict[str, Any]:
        self.descobrir(traceparent)
        return self._post(
            "tools/call",
            {"name": "reservar_sala", "arguments": arguments},
            nome="reservar_sala",
            traceparent=traceparent,
        )

    def retomar_reservar_sala(
        self,
        arguments: dict[str, Any],
        chave: str,
        resposta_elicitation: dict[str, Any],
        request_state: str,
        traceparent: str | None = None,
    ) -> dict[str, Any]:
        params = {
            "name": "reservar_sala",
            "arguments": arguments,
            "inputResponses": {chave: resposta_elicitation},
            "requestState": request_state,
        }
        return self._post(
            "tools/call",
            params,
            nome="reservar_sala",
            traceparent=traceparent,
        )


mcp_client = McpClient(MCP_URL)


# ============================================================================
# Modelo de Task e Gerenciador de Estado
# ============================================================================
class Task:
    def __init__(self, task_id: str, context_id: str):
        self.id = task_id
        self.context_id = context_id
        self.state = "TASK_STATE_SUBMITTED"
        self.history: list[dict[str, Any]] = []
        self.artifacts: list[dict[str, Any]] = []
        self.last_agent_message: dict[str, Any] | None = None

        # Estado da ponte (MRTR <-> Task) - Mantido isolado por Task e NUNCA exposto no A2A
        self.mcp_request_state: str | None = None
        self.mcp_input_key: str | None = None
        self.mcp_alternativas: list[str] = []
        self.mcp_original_arguments: dict[str, Any] | None = None
        self.traceparent: str | None = None

    def adicionar_mensagem_usuario(self, texto: str, message_id: str | None = None) -> dict[str, Any]:
        msg = {
            "messageId": message_id or f"msg-{secrets.token_hex(6)}",
            "role": "ROLE_USER",
            "parts": [{"text": texto}],
        }
        self.history.append(msg)
        return msg

    def definir_mensagem_agente(self, novo_estado: str, texto: str) -> dict[str, Any]:
        self.state = novo_estado
        msg = {
            "messageId": f"msg-{secrets.token_hex(6)}",
            "role": "ROLE_AGENT",
            "parts": [{"text": texto}],
            "taskId": self.id,
            "contextId": self.context_id,
        }
        self.last_agent_message = msg
        self.history.append(msg)
        return msg

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "contextId": self.context_id,
            "status": {
                "state": self.state,
                "message": self.last_agent_message,
            },
            "history": self.history,
            "artifacts": self.artifacts,
        }


# Tabela em memória de tarefas ativas
tarefas_por_id: dict[str, Task] = {}


def parse_pedido(texto: str) -> dict[str, str] | None:
    # Formato fixo: reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>
    padrao = r"reservar\s+sala=([^\s]+)\s+inicio=([^\s]+)\s+fim=([^\s]+)\s+responsavel=(.+)"
    m = re.match(padrao, texto.strip())
    if not m:
        return None
    return {
        "sala": m.group(1),
        "inicio": m.group(2),
        "fim": m.group(3),
        "responsavel": m.group(4).strip(),
    }


# ============================================================================
# Manipulador A2A JSON-RPC
# ============================================================================
def processar_send_message(rpc_id: Any, params: dict[str, Any], traceparent: str | None) -> dict[str, Any]:
    msg_data = params.get("message") or {}
    task_id = msg_data.get("taskId")
    partes = msg_data.get("parts") or []
    texto_usuario = " ".join(p.get("text", "") for p in partes).strip()

    # Cenário A: Nova Task (sem taskId)
    if not task_id:
        nova_task_id = f"task-{secrets.token_hex(6)}"
        novo_context_id = f"ctx-{secrets.token_hex(6)}"
        task = Task(nova_task_id, novo_context_id)
        tarefas_por_id[nova_task_id] = task

        task.adicionar_mensagem_usuario(texto_usuario, msg_data.get("messageId"))
        task.state = "TASK_STATE_WORKING"
        task.traceparent = traceparent

        args = parse_pedido(texto_usuario)
        if not args:
            task.definir_mensagem_agente("TASK_STATE_FAILED", "Pedido fora do formato esperado.")
            return {"jsonrpc": "2.0", "id": rpc_id, "result": {"task": task.to_dict()}}

        # Chamada ao servidor MCP via Streamable HTTP
        resp_mcp = mcp_client.chamar_reservar_sala(args, traceparent=traceparent)
        res_resultado = resp_mcp.get("result") or {}

        # Erro de execução da tool (ex: Sala inexistente, Fora da janela, etc)
        if res_resultado.get("isError"):
            msg_erro = " ".join(p.get("text", "") for p in res_resultado.get("content", []))
            task.definir_mensagem_agente("TASK_STATE_FAILED", msg_erro)
            return {"jsonrpc": "2.0", "id": rpc_id, "result": {"task": task.to_dict()}}

        # Conflito de reserva -> input_required com elicitation
        if res_resultado.get("resultType") == "input_required":
            pedidos = res_resultado.get("inputRequests") or {}
            chave = next(iter(pedidos), None)
            req_state = res_resultado.get("requestState")
            if not chave or not req_state:
                task.definir_mensagem_agente("TASK_STATE_FAILED", "Resposta input_required invalida do servidor MCP")
                return {"jsonrpc": "2.0", "id": rpc_id, "result": {"task": task.to_dict()}}

            # Extrair lista ordenada de alternativas
            params_pedido = pedidos[chave].get("params", {})
            props_sala = ((params_pedido.get("requestedSchema") or {}).get("properties") or {}).get("sala", {})
            enum_alts = props_sala.get("enum") or ([props_sala["const"]] if "const" in props_sala else [])

            # Guarda o estado na Task (A PONTE)
            task.mcp_request_state = req_state
            task.mcp_input_key = chave
            task.mcp_alternativas = enum_alts
            task.mcp_original_arguments = args

            # Mensagem exata de pausa exigida pelo validador
            msg_pausa = f"alternativas: {', '.join(enum_alts)}"
            task.definir_mensagem_agente("TASK_STATE_INPUT_REQUIRED", msg_pausa)
            return {"jsonrpc": "2.0", "id": rpc_id, "result": {"task": task.to_dict()}}

        # Sucesso direto (sala livre)
        if res_resultado.get("resultType") == "complete" and not res_resultado.get("isError"):
            structured = res_resultado.get("structuredContent") or {}
            reserva_id = structured.get("reserva")
            sala_confirmada = structured.get("sala")
            task.artifacts = [
                {
                    "artifactId": f"art-{secrets.token_hex(6)}",
                    "name": "reserva",
                    "parts": [
                        {
                            "text": json.dumps({
                                "reserva": reserva_id,
                                "sala": sala_confirmada,
                                "inicio": structured.get("inicio"),
                                "fim": structured.get("fim"),
                                "responsavel": structured.get("responsavel"),
                                "politica": mcp_client.politica_versao,
                            })
                        }
                    ],
                }
            ]
            task.definir_mensagem_agente(
                "TASK_STATE_COMPLETED",
                f"Reserva {reserva_id} confirmada na {sala_confirmada}.",
            )
            return {"jsonrpc": "2.0", "id": rpc_id, "result": {"task": task.to_dict()}}

        # Qualquer outro retorno inesperado
        task.definir_mensagem_agente("TASK_STATE_FAILED", "Resultado inesperado do servidor MCP")
        return {"jsonrpc": "2.0", "id": rpc_id, "result": {"task": task.to_dict()}}

    # Cenário B: Continuação referenciando taskId existente
    task = tarefas_por_id.get(task_id)
    if not task:
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": -32602, "message": f"Task nao encontrada: {task_id}"},
        }

    # Estado terminal é definitivo: não aceita novas mensagens
    if task.state in ("TASK_STATE_COMPLETED", "TASK_STATE_CANCELED", "TASK_STATE_FAILED"):
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": -32600, "message": f"Task {task_id} em estado terminal: {task.state}"},
        }

    if task.state != "TASK_STATE_INPUT_REQUIRED":
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": -32600, "message": f"Task {task_id} nao esta aguardando input"},
        }

    task.adicionar_mensagem_usuario(texto_usuario, msg_data.get("messageId"))

    # Interpretação da resposta: escolha=<id> ou escolha=recusar
    if not texto_usuario.startswith("escolha="):
        msg_pausa = f"alternativas: {', '.join(task.mcp_alternativas)}"
        task.definir_mensagem_agente("TASK_STATE_INPUT_REQUIRED", msg_pausa)
        return {"jsonrpc": "2.0", "id": rpc_id, "result": {"task": task.to_dict()}}

    valor_escolha = texto_usuario.split("escolha=", 1)[1].strip()

    # B1: Recusa pelo usuário
    if valor_escolha == "recusar":
        mcp_client.retomar_reservar_sala(
            arguments=task.mcp_original_arguments or {},
            chave=task.mcp_input_key or "",
            resposta_elicitation={"action": "decline"},
            request_state=task.mcp_request_state or "",
            traceparent=traceparent or task.traceparent,
        )
        task.definir_mensagem_agente("TASK_STATE_CANCELED", "Reserva recusada.")
        return {"jsonrpc": "2.0", "id": rpc_id, "result": {"task": task.to_dict()}}

    # B2: Escolha fora do enum mantém a Task pausada
    if valor_escolha not in task.mcp_alternativas:
        msg_pausa = f"alternativas: {', '.join(task.mcp_alternativas)}"
        task.definir_mensagem_agente("TASK_STATE_INPUT_REQUIRED", msg_pausa)
        return {"jsonrpc": "2.0", "id": rpc_id, "result": {"task": task.to_dict()}}

    # B3: Escolha válida -> retoma o servidor MCP com accept e o requestState
    resp_mcp = mcp_client.retomar_reservar_sala(
        arguments=task.mcp_original_arguments or {},
        chave=task.mcp_input_key or "",
        resposta_elicitation={"action": "accept", "content": {"sala": valor_escolha}},
        request_state=task.mcp_request_state or "",
        traceparent=traceparent or task.traceparent,
    )
    resultado = resp_mcp.get("result") or {}

    if resultado.get("isError"):
        msg_erro = " ".join(p.get("text", "") for p in resultado.get("content", []))
        task.definir_mensagem_agente("TASK_STATE_FAILED", msg_erro)
        return {"jsonrpc": "2.0", "id": rpc_id, "result": {"task": task.to_dict()}}

    structured = resultado.get("structuredContent") or {}
    reserva_id = structured.get("reserva")
    sala_confirmada = structured.get("sala") or valor_escolha
    task.artifacts = [
        {
            "artifactId": f"art-{secrets.token_hex(6)}",
            "name": "reserva",
            "parts": [
                {
                    "text": json.dumps({
                        "reserva": reserva_id,
                        "sala": sala_confirmada,
                        "inicio": structured.get("inicio"),
                        "fim": structured.get("fim"),
                        "responsavel": structured.get("responsavel"),
                        "politica": mcp_client.politica_versao,
                    })
                }
            ],
        }
    ]
    task.definir_mensagem_agente(
        "TASK_STATE_COMPLETED",
        f"Reserva {reserva_id} confirmada na {sala_confirmada}.",
    )
    return {"jsonrpc": "2.0", "id": rpc_id, "result": {"task": task.to_dict()}}


def processar_get_task(rpc_id: Any, params: dict[str, Any]) -> dict[str, Any]:
    task_id = params.get("id")
    task = tarefas_por_id.get(task_id)
    if not task:
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": -32602, "message": f"Task nao encontrada: {task_id}"},
        }
    return {"jsonrpc": "2.0", "id": rpc_id, "result": {"task": task.to_dict()}}


# ============================================================================
# Endpoints HTTP da Aplicação Starlette
# ============================================================================
async def endpoint_agent_card(request: Request) -> Response:
    card = {
        "name": "Central de Salas",
        "description": "Reserva salas de reuniao da Hill Valley Tech.",
        "provider": {
            "organization": "Hill Valley Tech",
            "url": "https://hillvalley.example",
        },
        "version": "1.0.0",
        "supportedInterfaces": [
            {
                "url": f"http://127.0.0.1:{AGENTE_PORT}/a2a",
                "protocolBinding": "JSONRPC",
                "protocolVersion": "1.0",
            }
        ],
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "extendedAgentCard": False,
        },
        "defaultInputModes": [
            "text/plain",
        ],
        "defaultOutputModes": [
            "text/plain",
        ],
        "skills": [
            {
                "id": "reservar-sala",
                "name": "Reservar sala",
                "description": "Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
                "tags": [
                    "salas",
                    "agenda",
                ],
                "inputModes": [
                    "text/plain",
                ],
                "outputModes": [
                    "text/plain",
                ],
                "examples": [
                    "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty",
                ],
            }
        ],
    }
    return JSONResponse(card)


async def endpoint_a2a(request: Request) -> Response:
    traceparent = request.headers.get("traceparent")
    try:
        corpo = await request.json()
    except Exception:
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})

    rpc_id = corpo.get("id")
    metodo = corpo.get("method")
    params = corpo.get("params") or {}

    if metodo == "SendMessage":
        resposta = processar_send_message(rpc_id, params, traceparent)
        return JSONResponse(resposta)
    elif metodo == "GetTask":
        resposta = processar_get_task(rpc_id, params)
        return JSONResponse(resposta)
    else:
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {"code": -32601, "message": f"Metodo nao suportado: {metodo}"},
            }
        )


routes = [
    Route("/.well-known/agent-card.json", endpoint_agent_card, methods=["GET"]),
    Route("/a2a", endpoint_a2a, methods=["POST"]),
]

app = Starlette(routes=routes)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=AGENTE_HOST, port=AGENTE_PORT, log_level="warning")
