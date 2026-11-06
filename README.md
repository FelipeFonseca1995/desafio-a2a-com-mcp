# A Ponte: um agente A2A com MCP por dentro

Desafio referente ao módulo Desenvolvimento de Agentes com IA (MBA Engenharia de Software com IA).

Implementação de fronteira entre dois protocolos sem sessão: um servidor **MCP** em Streamable HTTP expondo as capacidades de reserva de salas da Hill Valley Tech com suporte a Multi-Round-Trip (MRTR), e um **Agente A2A** que atua como host MCP por dentro e servidor A2A por fora, gerenciando o ciclo de vida de Tasks e a costura de estado.

---

## Como rodar

A partir de um clone limpo do repositório:

### 1. Pré-requisitos
- Python 3.10 ou superior

### 2. Criar ambiente virtual e instalar dependências

No Linux / macOS:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

No Windows (PowerShell):
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 3. Gerar e exportar a chave de integridade do requestState

Gere uma chave aleatória de no mínimo 32 bytes:
```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Exporte a chave gerada como variável de ambiente:

No Linux / macOS:
```bash
export REQUEST_STATE_SECRET="<chave_gerada_acima>"
```

No Windows (PowerShell):
```powershell
$env:REQUEST_STATE_SECRET="<chave_gerada_acima>"
```

> **Atenção:** Nunca versione o segredo. Utilize uma variável de ambiente em cada terminal onde o servidor MCP for executado.

### 4. Subir os processos

Abra dois terminais (com o ambiente virtual ativo e o `REQUEST_STATE_SECRET` definido):

**Terminal 1 — Servidor MCP (porta 7301):**
```bash
python3 servidor-mcp/server.py
```

**Terminal 2 — Agente A2A (porta 7300):**
```bash
python3 agente/main.py
```

### 5. Executar o validador

Com os dois processos recém-iniciados, em um terceiro terminal:
```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

---

## Onde a ponte acontece

A costura entre os dois protocolos desacoplados ocorre no arquivo [`agente/main.py`](file:///agente/main.py):

1. **Do MCP `input_required` para o A2A `TASK_STATE_INPUT_REQUIRED`:**
   Na função `processar_send_message` (linhas 188 a 211), ao chamar o endpoint MCP `tools/call` para `reservar_sala` e receber uma resposta com `resultType == "input_required"`, o agente intercepta o pacote antes de responder ao cliente A2A. Ele extrai o `requestState` opaco e a chave da solicitação (`inputRequests`), obtém as salas alternativas do schema dinâmico em form mode (`requestedSchema.properties.sala.enum`), armazena esses dados internamente associados àquela `Task` (`task.mcp_request_state`, `task.mcp_input_key`, `task.mcp_alternativas`, `task.mcp_original_arguments`) e coloca a Task no estado `TASK_STATE_INPUT_REQUIRED`. A resposta ao cliente A2A devolve estritamente a mensagem formatada `alternativas: <ids ordenados>`, mantendo o `requestState` completamente oculto.

2. **Do A2A de continuação de volta para o servidor MCP:**
   Ainda em `processar_send_message` (linhas 250 a 286), quando o cliente envia um novo `SendMessage` contendo o `taskId` e o comando `escolha=<id_da_sala>` (ou `escolha=recusar`), o agente recupera a Task pausada. Ele valida se a escolha pertence ao enum gravado previamente e dispara um novo `tools/call` para o servidor MCP com um JSON-RPC `id` inédito. Nesse retry, o agente ecoa o `requestState` original sem adulteração juntamente com o mapa `inputResponses` (`action: "accept"` com `content: {"sala": valor}` ou `action: "decline"`). O servidor MCP verifica a integridade do estado via HMAC/AES-GCM, recupera os argumentos selados e conclui a operação, permitindo que a Task avance deterministicamente para `TASK_STATE_COMPLETED` (com o artifact de reserva) ou `TASK_STATE_CANCELED`.

---

## Decisões técnicas

1. **Proteção e Integridade do `requestState`:**
   O servidor MCP utiliza a suíte de segurança nativa do SDK oficial `mcp 2.3.0` (`RequestStateSecurity` e `RequestStateBoundary`), configurada com `AES-256-GCM` via biblioteca `cryptography`. A chave de rotação é carregada diretamente da variável `REQUEST_STATE_SECRET` (validando o tamanho mínimo de 32 bytes). O envelope selado protege a expiração (TTL fixado em 10 minutos / 600 segundos), método, digest criptográfico dos argumentos da tool (`args_digest`) e dados internos do resolver. Se o token for adulterado ou se os argumentos no body divergirem do selo original, o envelope rejeita a solicitação com erro `-32602` e HTTP 400. Como o estado trafega inteiramente selado no token, o servidor é 100% stateless: se o processo do servidor for reiniciado entre o `input_required` e o retry, a operação retoma e conclui normalmente.

2. **Tempo de Expiração (TTL):**
   O TTL configurado para o token é de **600 segundos (10 minutos)**, respeitando a janela estipulada pela especificação e pelo enunciado (entre 5 e 30 minutos).

3. **Gerenciamento e Isolamento do Estado das Tasks no Agente:**
   O agente mantém uma tabela em memória `tarefas_por_id: dict[str, Task]`. Cada instância de `Task` encapsula de forma independente seus identificadores (`id`, `contextId`), histórico de mensagens, artifacts e os metadados de protocolo MCP (`mcp_request_state`, `mcp_input_key`, `mcp_alternativas`, `mcp_original_arguments`). Ao serializar a Task para as respostas JSON-RPC (`to_dict()`), apenas os campos padrão da especificação A2A v1.0 (`id`, `contextId`, `status`, `history`, `artifacts`) são serializados, assegurando que o `requestState` nunca vaze para o cliente A2A. Tarefas concorrentes em pausa mantêm seus estados estritamente isolados em sua respectiva instância, sem colisão de variáveis globais.

4. **Propagação de Contexto (Traceparent):**
   Quando o cabeçalho W3C `traceparent` é fornecido na chamada A2A, o agente preserva o mesmo `trace-id` e o propaga dentro do campo `_meta.traceparent` em todas as requisições MCP subsequentes associadas àquela tarefa. O servidor MCP possui um middleware ASGI assíncrono que intercepta todo tráfego de entrada e registra no `sys.stderr` o método, o ID da requisição e o `traceparent`, viabilizando a auditoria distribuída de ponta a ponta.

---

## Saída do validador

Abaixo, a saída completa da execução do validador oficial (`validador/validar.py`):

```
trace-id desta execucao: e207a325d90b3ea53578a76ec17462c9
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```
