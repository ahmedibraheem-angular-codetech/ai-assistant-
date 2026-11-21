# Handoff — Personal AI Assistant (state at 2026-11-21)

Read this first in a new session. The running project log is also in the
assistant's own project memory (`GET /api/v1/projects/personal%20assistant/brief`).

## Where things are

| Item | Value |
|---|---|
| Repo (on the Mac) | `/Users/a.m.ibraheem/Desktop/local-mac-ai-assistant/personal-ai-assistant` |
| GitHub | `ahmedibraheem-angular-codetech/ai-assistant-`, branch `wp02-spawn-unreaped-remediation` (all pushed) |
| Python | `.venv/bin/python` (3.15, macOS venv — use the Mac shell via Desktop Commander, not a Linux VM) |
| Tests | `.venv/bin/python -m pytest -q -p no:cacheprovider -W ignore::DeprecationWarning` → **603 passed, 12 skipped** |
| Live Docker tests | `RUN_DOCKER_TESTS=1 ... tests/test_sandbox.py` |
| Run server | `.venv/bin/python -m app --port 8000` (token: `.venv/bin/python -m app.control_plane token`) |
| DB | `data/assistant.db`, **schema 27**; backups `data/assistant.db.backup-before-vNN` |
| Mac app | `desktop/` (Angular 21 + Tauri 2). `CI=1 npx ng test --watch=false` → **132 passed**; `cd src-tauri && cargo test` → **26 passed**; `npm run check:freeze` after adding a package |
| Run app | server first, then `cd desktop && ASSISTANT_API_PORT=8765 npx tauri dev` (dev server pinned to 127.0.0.1:4200) |
| Old design/review docs | `~/.cursor/projects/Users-a-m-ibraheem-Desktop-local-mac-ai-assistant-personal-ai-assistant/agent-tools/` |

## Done (phases 0–4)

- **0** Work committed and pushed; fixed 20 failing tests left by WP-04b (research tool was fully broken).
- **1** WP-05A simplified: Bearer token on every route except health; loopback Host/Origin checks; `rotate` CLI refused while serving; one serve lease inside the lifespan. WP-04 leftovers: R-01 EINTR, R-17 `MCP_MAX_PROCESSES`, R-02/R-03 documented.
- **2** Agents engine: standing approvals (reads only, `ask_sensitive` only); kill switch + daily budgets; team delegation (`delegate_task`, one level, explicit teams); hiring studio (6 templates); Docker sandbox `run_code` (no network/files, approval every run).
- **3** Memory & knowledge: project memory (`project_brief`, `record_project_entry`); hybrid search (vector + Arabic-aware keywords); image OCR via macOS Vision through `osascript` JXA; `ingest_folder` with sealed file list; knowledge graph (`graph_lookup`, `graph_record`). Decision: stay on SQLite.
- **4** Self-learning: lessons + feedback (`record_lesson`; lessons after untrusted content are `pending`); versioned skills gated by per-agent eval cases (`save_skill`, evaluate/promote/reject/rollback, agent profile "CV"); scheduled study time (background scheduler, pending lessons, study digest).

README.md documents every API with curl examples.

## Phase 5 progress — Mac app

Done (each slice committed and pushed):
- **Slice 1** scaffold + token bridge (`api_request`; Rust reads the token file per request, loopback only, no proxy/redirects) + chat page.
- **Slice 2** `GET /api/v1/analytics/summary` + analytics home page (KPI rings, assistant map, latest actions, SVG activity chart, tools, learning, range switch, pause/resume), modern dark/light shell. Design canvas: https://claude.ai/artifact/XVeAQyt9zx2bqufBpvkRnw
- **Slice 3** approvals page (pending + history, sealed arguments, change preview, integrity warnings, two-click approve, menu badge).
- **Slice 4** team page, hiring from templates, agent profile (review lessons/skills, evaluations, study plan, team members, stop/start).
- **Slice 5** customize page for the home widgets (show/hide, order, density, motion), saved locally.
- **Slice 6** projects (brief, tasks, entries, archive), knowledge graph (entities, neighbour drawing, relations), study digest on the team page.
- **Slice 7** desktop shell: ⌥ Space quick window, Menu Bar menu (open, quick, approvals, pause/resume, launch at login, quit), close hides. All in Rust (`desktop.rs`); the tray icon is declared in `tauri.conf.json` because `tauri dev` rewrites Cargo features from the config.
- **Slice 8** packaging: `npm run dist` → `Personal Assistant.app` + `.dmg`, ad-hoc signed (no Developer ID on this Mac), hardened runtime. The built app uses port 8000 and the repo's `data/` (path fixed at build time).
- **Fix** concurrent chat turns returned 500 (SQLite single writer while a turn holds its transaction): one chat turn at a time per server.
- **Phase 7 slice 1 (done)** scheduled routines: schema 26 `routines`, `app/routines.py` (pure `next_due`/`is_due`, 30 s scheduler, catch-up once within `ROUTINE_CATCHUP_HOURS`, tz `ROUTINE_TIMEZONE`), admin-only `/api/v1/routines` (+`/run`), Mac page `/routines`. Runs go through `ConversationService.send` (pause, budgets, approvals unchanged); agents cannot create routines. Initial bundle budget warning raised to 520 kB; the routines page is lazy-loaded.
- **Phase 7 slice 2 (done)** n8n: schema 27 `n8n_workflows`, `app/n8n.py` (model, loopback base-URL check, `RunN8nWorkflowTool`), admin-only `/api/v1/n8n/workflows` + `/n8n/status`, Settings section `desktop/src/app/settings/n8n-section.*`, approvals card shows the registered workflow. `run_n8n_workflow` is declared like `run_code` (manifest risk sensitive, always asks, never standing) and is NOT in any template: grant it per agent. Later item: inbound calls from n8n to the assistant (out of scope so far).
- **Phase 7 browser slice 1 (done)** `app/browser.py`: `browser_read` (read only) in headless installed Chrome via Playwright, empty profile, every call asks (risk sensitive, never standing, not in templates). All traffic goes through `FilterProxy` (public IPs only, resolve once and connect to the checked IP); route interception alone was NOT enough (Chrome follows redirects, even fulfilled 3xx, without re-routing; proven by `tests/test_browser.py`, run with `BROWSER_TESTS=1`). Next: click/type actions, each one approved.
- **Phase 7 slice 3 (done)** proactive insights: `app/insights.py` (`collect`, isolated rules), admin-only `GET /api/v1/insights`, home widget `insights` + menu badge (`core/insights.ts`), Rust poller `src-tauri/src/notify.rs` (5 min, notifies new high ids). No schema change (still 27).
- **Phase 8 (done: style profile)** writing style profile without fine-tuning: `app/style.py`, admin-only `/api/v1/style` and `/style/dataset(/export)`, injected in `ConversationService.send` (default assistant) and `AgentExecutionService.execute` (agents); Mac page `/style` (lazy). **LoRA deferred until >=300 pairs**; `data/training` export is ready (M4 Pro 24 GB; mlx 0.32 has cp315 wheels; nothing downloaded). No schema change (still 27).
- **Schema 25** `tool_runs` log (every started run; written in the bound session during a turn); analytics read it.

Lessons from this phase (keep):
- The window runs with `freezePrototype: true`. echarts breaks it silently (the app never starts); charts are plain SVG. Run `npm run check:freeze` for any new package.
- `localhost:4200` resolved to IPv6 only and the window loaded nothing; dev server and devUrl are pinned to 127.0.0.1.
- A chat turn must never hold SQLite's write lock while a model answers: the user message is committed before the model call, tool writes are committed after each run, and a failed turn deletes what it committed early (no half turn). Chat turns are serialized. Pause answers in ~50 ms during a turn; it blocks tools and new turns but lets an in-flight model reply finish.
- Computer use is not set up on the Mac, so the window cannot be screenshotted; verify through the server log or the built-in browser at 127.0.0.1:4200.

## Next: phase 5 — remaining slices

Floating window on a global shortcut, Menu Bar icon, starts with macOS; pages
for chat, conversations, approvals inbox, team + agent profiles
(skills/lessons/evals), study digest, projects, graph, settings (pause,
budgets, standing approvals, hiring). Talks to the local API with the Bearer
token (read the token file through a Tauri command; never ship it in the web
bundle). Rust/cargo is installed. Needs npm downloads.

Phase 5 is done. The app now starts Ollama and the API when they are down (`desktop/src-tauri/src/services.rs`; it prefers the `com.personal-assistant.server` LaunchAgent, installed on this Mac, through `launchctl kickstart`). Release builds must pass `npm run check:csp` (inline critical CSS and Tauri style nonces left the app unstyled). Plan from the user: everything stays local for now; later the backend moves to a host, `ASSISTANT_API_URL` now exists for that (https required off-loopback; when it is not loopback the app skips all server management). Open items: Developer ID signing + notarization if the app is shared; `npm run dist` sometimes fails at the dmg step ("Resource busy"), rerun `npx tauri bundle --bundles dmg`.
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
