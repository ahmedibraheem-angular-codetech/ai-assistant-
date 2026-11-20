# Handoff — Personal AI Assistant (state at 2026-11-20)

Read this first in a new session. The running project log is also in the
assistant's own project memory (`GET /api/v1/projects/personal%20assistant/brief`).

## Where things are

| Item | Value |
|---|---|
| Repo (on the Mac) | `/Users/a.m.ibraheem/Desktop/local-mac-ai-assistant/personal-ai-assistant` |
| GitHub | `ahmedibraheem-angular-codetech/ai-assistant-`, branch `wp02-spawn-unreaped-remediation` (all pushed, last commit `ed2767f`) |
| Python | `.venv/bin/python` (3.15, macOS venv — use the Mac shell via Desktop Commander, not a Linux VM) |
| Tests | `.venv/bin/python -m pytest -q -p no:cacheprovider -W ignore::DeprecationWarning` → **538 passed, 12 skipped** |
| Live Docker tests | `RUN_DOCKER_TESTS=1 ... tests/test_sandbox.py` |
| Run server | `.venv/bin/python -m app --port 8000` (token: `.venv/bin/python -m app.control_plane token`) |
| DB | `data/assistant.db`, **schema 24**; backups `data/assistant.db.backup-before-vNN` |
| Old design/review docs | `~/.cursor/projects/Users-a-m-ibraheem-Desktop-local-mac-ai-assistant-personal-ai-assistant/agent-tools/` |

## Done (phases 0–4)

- **0** Work committed and pushed; fixed 20 failing tests left by WP-04b (research tool was fully broken).
- **1** WP-05A simplified: Bearer token on every route except health; loopback Host/Origin checks; `rotate` CLI refused while serving; one serve lease inside the lifespan. WP-04 leftovers: R-01 EINTR, R-17 `MCP_MAX_PROCESSES`, R-02/R-03 documented.
- **2** Agents engine: standing approvals (reads only, `ask_sensitive` only); kill switch + daily budgets; team delegation (`delegate_task`, one level, explicit teams); hiring studio (6 templates); Docker sandbox `run_code` (no network/files, approval every run).
- **3** Memory & knowledge: project memory (`project_brief`, `record_project_entry`); hybrid search (vector + Arabic-aware keywords); image OCR via macOS Vision through `osascript` JXA; `ingest_folder` with sealed file list; knowledge graph (`graph_lookup`, `graph_record`). Decision: stay on SQLite.
- **4** Self-learning: lessons + feedback (`record_lesson`; lessons after untrusted content are `pending`); versioned skills gated by per-agent eval cases (`save_skill`, evaluate/promote/reject/rollback, agent profile "CV"); scheduled study time (background scheduler, pending lessons, study digest).

README.md documents every API with curl examples.

## Next: phase 5 — Mac app (Angular + Tauri 2)

Floating window on a global shortcut, Menu Bar icon, starts with macOS; pages
for chat, conversations, approvals inbox, team + agent profiles
(skills/lessons/evals), study digest, projects, graph, settings (pause,
budgets, standing approvals, hiring). Talks to the local API with the Bearer
token (read the token file through a Tauri command; never ship it in the web
bundle). Rust/cargo is installed. Needs npm downloads.

Slices: (1) scaffold Tauri + Angular, token bridge, chat page; (2) approvals
inbox + pause button; (3) team/profile/hiring pages; (4) projects/graph/study
digest; (5) floating window, shortcut, Menu Bar, autostart; (6) packaging.
Later: 6 voice + mobile, 7 automation (n8n, browser, proactive tasks),
8 optional LoRA fine-tuning, 9 hardening.

## Working rules (keep them)

1. Small slices: read code → implement → focused tests → **full suite** → live check on port 8765 when startup or schema changes → README section → commit → push.
2. **Before any schema change on the real DB: copy `data/assistant.db` to `data/assistant.db.backup-before-vNN`.**
3. New schema version: bump `CURRENT_SCHEMA_VERSION`, add `upgrade_to_NN` in `app/core/migrations.py`, register models in `init_database`, update version asserts in `test_knowledge.py`, `test_research.py`, `test_agents.py`, and version lists in `test_conversations.py`, `test_tools.py`, `test_memories.py`, `test_permissions.py`.
4. Security invariants: agents never change permissions; untrusted content is data (`data_boundary` tags); agent writes are append-only and labeled; mutations/commands/sandbox always ask; tools that write during a chat reuse the request's bound DB session (`app.agents.team._session_scope`).
5. Test helpers: `tests/client_support.LocalClient`, `tests/approval_support.finish_tool_test` / `prime_executing_approval`, `tests/test_tools.scripted_chat` / `tool_call`. Always stub the model with `scripted_chat`. Messages starting with "سجل/افتكر" hit the memory-intent handler before the model.

## Environment notes

- Command Line Tools install is broken (`swiftc` fails: duplicate `SwiftBridging` module map). Fix needs admin: remove `/Library/Developer/CommandLineTools`, then `xcode-select --install`.
- Docker 29.9 running; sandbox images pulled.
- Off by default: `WEB_SEARCH_ENABLED`, `SANDBOX_ENABLED`. Scheduler on (`STUDY_SCHEDULER_ENABLED`).
