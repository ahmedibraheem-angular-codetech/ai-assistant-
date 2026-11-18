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

Run the API on localhost only. The API has no authentication. Do not bind it to
`0.0.0.0`, expose it through a tunnel, or use it remotely until authentication
and authorization exist. A workspace id is not a security boundary.

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

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
