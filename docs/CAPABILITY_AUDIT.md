# Capability audit: the autonomous-assistant goal

Date: 2026-11-21. Checked against the running code (tool list from `/api/v1/tools`, modules in `app/`), not the docs.
Constraint from the owner: **no paid services**. Everything runs on this Mac with free/local software.

Goal: "tell the assistant what you want, and it researches, plans, builds the team and tools, executes and checks, and comes back only for decisions."

| # | Capability | Status | Present | Missing |
|---|---|---|---|---|
| 1 | Do any task (e.g. a full coding project) | PARTIAL | file read/create/update (approval), fixed safe commands (`inspect_project`, `show_directory`, ...), `run_code` sandbox (off by default), `delegate_task` | a coding agent loop: scaffold, install, build, test, fix, repeat; approved commands inside a project workspace |
| 2 | Add tools/plugins by itself | MISSING | MCP servers and n8n workflows added by the owner only | discover a need, propose an MCP server / n8n workflow, install after approval, test before use |
| 3 | Web and social media like Claude | PARTIAL | `browser_read` (read only, public sites, filtering proxy), `web_search` (DuckDuckGo instant answers: weak), `research` | browser actions (click, fill, sign-in sessions) with per-action approval; free better search (self-hosted SearXNG in Docker); Instagram via the free official Graph API (Business/Creator account) |
| 4 | Create agents by itself | PARTIAL | 6 templates, admin-only `/agents/hire`, study plans, lessons and skills, per-agent `model` field | a tool for the assistant to propose a new agent (custom role, tools, study plan, model) that waits for approval; agents beyond the 6 templates |
| 5 | Reach goals with little intervention | MISSING (core gap) | routines, insights, approvals, delegation | a goals engine: clarify, plan (steps, dependencies, agents), owner approves the plan once, run in the background for days, check each step, report, ask only at decisions |

## Order

1. Goals engine (planner model setting `GOAL_PLANNER_MODEL`, default the local Qwen 3.5 9B; a stronger free local model such as `gpt-oss:20b` or `qwen3:14b` can be swapped in later).
2. The assistant proposes new agents (approval).
3. Coding agent workspace (approved commands, build/test loop).
4. Capability manager (propose and install MCP servers / n8n workflows, approval).
5. Browser actions, SearXNG search, Instagram Graph API.

## Model note

The local 9B model answers well but plans weakly. The goals engine compensates with JSON-schema outputs (Ollama `format`), small steps, one plan approval by the owner, and a check of every step result before moving on.
