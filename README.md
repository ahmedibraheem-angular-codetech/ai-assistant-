# Personal AI Assistant — AI Backend

Local-first backend foundation. This first slice includes:
- FastAPI application
- Ollama connectivity and installed-model listing
- Chat endpoint
- Basic tests

## Requirements
- Python 3.11+
- Ollama installed and running (`ollama serve`, if it is not already running)

## Setup (macOS)

```bash
cd personal-ai-assistant
python3 --version
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
cp .env.example .env
```

If Ollama is not running, start it in another terminal:

```bash
ollama serve
```

Check installed models:

```bash
ollama list
```

Copy the exact chat model name from that output into `.env` as `OLLAMA_MODEL=...`.
Do not use an embedding-only model for chat.

Run the API on localhost only:

```bash
python -m app            # http://127.0.0.1:8000
```

### Authentication

Every request needs `Authorization: Bearer <token>`, except
`GET /api/v1/health`. The token is created on first start in `data/control_plane.token`
(mode 0600). Requests must use a localhost address, and browser requests from
other sites are refused. Do not bind to `0.0.0.0` or expose the API through a tunnel.

```bash
python -m app.control_plane token    # print the token
python -m app.control_plane rotate   # new token (stop the server first)

TOKEN=$(python -m app.control_plane token)
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/api/v1/agents
```

`/docs` is protected too. Use it from a client that can send the header.

File tools are read-only and can see only directories registered through
`/api/v1/workspaces`. The home directory and the disk root are not registered
automatically. macOS may still deny protected folders such as Desktop or
Documents. The app does not bypass those privacy protections. Authentication
is required before any remote access.

Open:
- API docs: http://127.0.0.1:8000/docs
- Health: http://127.0.0.1:8000/api/v1/health
- Ollama models: http://127.0.0.1:8000/api/v1/models

## First chat request

```bash
curl -X POST http://127.0.0.1:8000/api/v1/chat \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message":"عرّف نفسك في سطرين باللهجة المصرية"}'
```

## Current scope
This is intentionally the first vertical slice, not the complete multi-agent system.
Next modules: configuration/core, model registry and per-agent model assignment, agent registry/runtime, orchestrator, memory, tools, permissions, task engine, audit logs.

## CI — WP-02 on Linux

GitHub Actions workflow `.github/workflows/wp02-linux.yml` runs the WP-02 process
security tests on Ubuntu (`ubuntu-latest`), including real `execveat` coverage that
cannot be verified on macOS alone.

### Standing approvals (reads only)

By default every file read, file search, and web research asks for approval.
A standing approval is your lasting yes for one agent, one read-only tool, and
one scope, so the agent can work without asking each time:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/permissions/standing-approvals \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"agent_id":"<agent>","tool_name":"read_file","workspace_id":"<workspace>","path_prefix":"docs","expires_in_days":30}'
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/api/v1/permissions/standing-approvals
curl -X DELETE -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/api/v1/permissions/standing-approvals/<id>
```

Limits: reads only (creating, changing, or deleting files, commands, and
sensitive tools always ask); only in `ask_sensitive` mode (`always_ask` keeps
asking); tool, workspace, and path grants still apply; sensitive files such as
`.env` stay blocked. Every add and revoke is written to the permission audit.

### Pause (kill switch) and daily budgets

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/api/v1/control/pause \
  -H "Content-Type: application/json" -d '{"reason":"stop everything"}'
curl -X POST -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/api/v1/control/resume
curl -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  http://127.0.0.1:8000/api/v1/control/agents/<agent>/budget -d '{"daily_tool_calls":200,"daily_chat_turns":100}'
```

While paused, no tool starts, no model call is made, and running file workers
are killed. The pause survives restarts. Budgets are daily (UTC) per agent;
`AGENT_DAILY_TOOL_CALLS` / `AGENT_DAILY_CHAT_TURNS` set a default for all
agents. Usage counters reset when the server restarts.

### Teams (delegation)

A manager agent can hand sub-tasks to members of its team with the
`delegate_task` tool:

```bash
curl -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  http://127.0.0.1:8000/api/v1/agents/<manager>/team -d '{"member_ids":["<member>"]}'
# then grant the manager the tool:
curl -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  http://127.0.0.1:8000/api/v1/agents/<manager>/tools -d '{"allowed_tools":["delegate_task"]}'
```

Members keep their own tools, workspaces, approvals, and budgets; delegation
never widens permissions. Members cannot delegate further. A member's answer
reaches the manager as untrusted data.

### Hiring studio (agent templates)

```bash
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/api/v1/agent-templates
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  http://127.0.0.1:8000/api/v1/agents/hire \
  -d '{"template_id":"marketing","name":"Ads","workspace_ids":[]}'
```

Templates: `marketing`, `developer`, `researcher`, `secretary`, `analyst`,
`manager`. Hiring creates a normal, editable agent with the template's
instructions, only the template's tools, and a suggested daily budget. It does
not create standing approvals; add those yourself if you want them.

### Code sandbox (`run_code`)

Agents can run short Python, Node, or shell programs in a throwaway Docker
container. Off by default; turn it on in `.env`:

```bash
SANDBOX_ENABLED=true
docker pull python:3.12-alpine node:22-alpine alpine:3.20   # once
```

Every run asks for your approval (standing approvals never apply). The
container has no network, no access to your files, a read-only filesystem, a
non-root user, no capabilities, memory/CPU/process limits, a timeout, and an
output cap, and it is deleted after the run. Live tests: `RUN_DOCKER_TESTS=1 pytest tests/test_sandbox.py`.

## Known limits (by design)

- **MCP call timeouts (R-02):** if an MCP tool call times out, its result is
  reported as `unknown`. The server is not told to cancel, so the remote action
  may still have happened. Check before retrying anything with side effects.
- **Stopping a tool does not undo it (R-03):** a timeout, cancel, or kill stops
  the worker but cannot undo work it already finished. File changes made
  through the assistant are journaled and can be restored from
  `/api/v1/file-recoveries`; other side effects cannot.
- **MCP process limit (R-17):** at most `MCP_MAX_PROCESSES` (default 2) MCP
  servers run at once.
