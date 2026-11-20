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

### Project memory

Each project keeps decisions, tasks, notes, and the current status, so an
agent can resume work ("كمل اللي كنا فيه") without a re-explanation:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  http://127.0.0.1:8000/api/v1/projects -d '{"name":"Website","description":"..."}'
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  http://127.0.0.1:8000/api/v1/projects/website/entries -d '{"kind":"decision","text":"..."}'
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/api/v1/projects/website/brief
```

Agents use `project_brief` (read) and `record_project_entry` (append-only:
add entries or mark a task done; no edit or delete). Agent entries are labeled
and you can delete any entry. Hired agents get both tools.

### Knowledge search: hybrid ranking and image text

`search_knowledge` ranks by meaning (embeddings) mixed with keyword overlap,
so exact names, codes, and numbers are found. Arabic spelling variants are
matched (أ/إ/ا, ة/ه, ى/ي, diacritics, Arabic-Indic digits, the "ال" article).

Images (`.png .jpg .jpeg .heic .tiff .bmp .gif .webp`) can be ingested: their
text is read locally with macOS Vision OCR (Arabic and English), with no
download or extra package.

`ingest_folder` adds a whole workspace folder with one approval. The approval
lists every file with its hash; a file that changes afterwards is skipped and
a file added afterwards is not included. Sensitive files (`.env`, keys,
databases) and unsupported types are never included. Max 100 files per run.

### Knowledge graph

People, projects, organizations, places, and topics, and how they relate:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  http://127.0.0.1:8000/api/v1/graph/entities -d '{"type":"person","name":"أحمد"}'
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  http://127.0.0.1:8000/api/v1/graph/relations -d '{"subject":"أحمد","relation":"مسؤول عن","object":"Website"}'
curl -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8000/api/v1/graph/lookup?name=احمد"
```

Names match across Arabic spelling variants. Agents use `graph_lookup` (read)
and `graph_record` (append-only; cannot edit, delete, or overwrite your
descriptions). Hired agents get both tools.

### Agent lessons and feedback (self-learning, part 1)

Agents save short lessons with `record_lesson`; related lessons are added to
their prompt on similar tasks. Your feedback teaches them too:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  http://127.0.0.1:8000/api/v1/agents/<agent>/feedback -d '{"rating":"down","comment":"Keep answers short."}'
curl -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8000/api/v1/agents/<agent>/lessons?status=pending"
curl -X PATCH -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  http://127.0.0.1:8000/api/v1/lessons/<id> -d '{"status":"active"}'   # or "retired"
```

A lesson recorded in a turn that read untrusted content (web, files,
knowledge, other agents, program output) is saved as `pending` and is not
used until you approve it, so a malicious page cannot plant a lasting rule.
Lessons are reference data and never change permissions.

### Skills and evaluations (self-learning, part 2)

Agents save reusable procedures with `save_skill`. Each save is a new version
and starts as a **candidate**; it is used only after it passes the agent's
evaluation cases (or you activate it). Evaluations run the model without
tools (no side effects), compare current skills vs the candidate, and promote
only if the score does not drop; otherwise the candidate is rejected and the
old version stays.

```bash
# a test the agent must pass: the answer must include "EGP"
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  http://127.0.0.1:8000/api/v1/agents/<agent>/eval-cases -d '{"prompt":"Summarize invoice 7","must_include":["EGP"]}'
curl -X POST -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/api/v1/skills/<skill>/evaluate
curl -X POST -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/api/v1/skills/<skill>/rollback
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/api/v1/agents/<agent>/profile   # the agent's CV
```

Skills saved after untrusted content start as `pending` (your review first).
Evaluations respect the pause switch and daily budgets.

### Study time (self-learning, part 3)

Give an agent topics and an interval; a background scheduler runs its study
sessions:

```bash
curl -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  http://127.0.0.1:8000/api/v1/agents/<agent>/study-plan -d '{"topics":["facebook ads","copywriting"],"interval_hours":24}'
curl -X POST -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/api/v1/agents/<agent>/study-now
curl -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8000/api/v1/study/digest?hours=24"   # what the team learned
```

A session evaluates candidate skills, researches up to 3 topics (rotating),
and saves findings as **pending** lessons for your review (web content is
untrusted). Research goes through the normal permission gate: without a
standing approval for `research`, the session leaves an approval request
instead. Pause and budgets apply. Turn the scheduler off with
`STUDY_SCHEDULER_ENABLED=false`.

## Analytics (dashboard data)

One read-only call returns everything the Mac app's home page shows:

```bash
curl -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8000/api/v1/analytics/summary?range=7d"   # today | 7d | 30d
```

- `kpis`: chat turns and new conversations (with the previous period),
  tool actions with success rate, pending approvals (and the oldest), new
  lessons, today's budget use.
- `agents`: each agent with its team members, turns, tool actions, pending
  lessons, active skills, study runs, last activity and today's usage.
- `systems`: memory, knowledge, projects, graph, denied tool calls.
- `queue`: the latest 8 approval requests. `trend`: hourly (today) or daily
  buckets of chat turns and finished tool actions. `tools`: most used tools.
  `learning`: lessons, skills, study runs, new memories.

Limits: tool numbers come from approval requests and the permission audit.
Tool calls that run without an approval are not logged yet, so they are not
counted. Budget use is today's in-memory counter (it resets on restart).

## Mac app (phase 5, in progress)

`desktop/` is an Angular + Tauri 2 app with a side menu, an analytics home
page and the chat page. The home page reads `/api/v1/analytics/summary`:
KPI rings, a map of the assistant with its agents and systems (click a node
for its numbers), the latest approval actions, an activity chart, the most
used tools, learning and memory, a range switch (today / 7 days / 30 days),
and a pause/resume button. It refreshes every 30 seconds. Dark and light
themes switch from the side menu; all colours are tokens in
`desktop/src/styles.css`. Start the server, then:

```bash
cd desktop
npm install                      # first time only
npx tauri dev                    # opens the window; talks to port 8000
ASSISTANT_API_PORT=8765 npx tauri dev   # or another port
```

**The web view never sees the token.** It calls one Tauri command,
`api_request(method, path, body)`. Rust reads `data/control_plane.token`
again for each request (so a `rotate` works without a restart), with the same
checks as the server: private regular file, owned by you, not a symlink or
hard link, 43 characters. Rust then calls `http://127.0.0.1:<port>` itself.
The host is fixed to loopback. Only `/api/v1/...` paths and the usual methods
are allowed, and the request uses no proxy and does not follow redirects. The
request comes from Rust, so it has no browser `Origin`. The CSP blocks the
web view from making any network request of its own. `ASSISTANT_DATA_DIR`
changes the token folder (default: the repo's `data/`).

Tests:

```bash
cd desktop && CI=1 npx ng test --watch=false          # Angular (vitest)
cd desktop/src-tauri && cargo test                    # token bridge
ASSISTANT_API_PORT=8765 cargo test --test live -- --ignored --nocapture   # live, needs server + Ollama
```

The live test sends one real chat and then deletes that conversation.

**Adding a front-end package:** the window runs with `freezePrototype: true`
(Object.prototype is read-only, a defence against prototype pollution). A
library that writes to it at load time stops the whole app with no visible
error. Run `npm run check:freeze` in `desktop/` after adding one. echarts
fails this check, so charts are plain SVG (`src/app/shared/line-chart.ts`).
The dev server is pinned to `127.0.0.1:4200`: on some Macs `localhost`
resolves only to IPv6 and the window would load nothing.

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
