# Personal Assistant — Mac app

Angular 21 + Tauri 2 front end for the local assistant API. See the
"Mac app" section of the main `README.md` for how the token bridge works.

```bash
npm install
npx tauri dev                           # window, server on port 8000
ASSISTANT_API_PORT=8765 npx tauri dev   # another port
CI=1 npx ng test --watch=false          # Angular tests
(cd src-tauri && cargo test)            # bridge tests
npm run check:freeze                    # after adding a package (see below)
```

Layout:

- `src/app/app.*`: shell (side menu, theme switch, page outlet).
- `src/app/dashboard/`: analytics home page. `dashboard-view.ts` turns the
  server summary into cards, map nodes, rows and chart lines (pure functions,
  unit-tested); a new widget is one more builder there.
- `src/app/shared/line-chart.ts`: SVG line chart (tooltip, screen-reader table).
- `src/app/core/layout.ts` + `src/app/customize/`: home widgets, order, density (add a widget to `WIDGETS` and a `@case` in the dashboard).
- `src/app/projects/`, `src/app/knowledge/`: project briefs and the knowledge graph (`knowledge-view.ts` holds the rules).
- `src/app/team/`: team page, hiring, and agent profile (`team-view.ts` holds the rules).
- `src/app/approvals/`: approvals page (`approvals-view.ts` holds the rules: risk, status, whether approve is offered).
- `src/app/core/approvals-store.ts`: pending list shared by the menu badge and the page.
- `src/app/chat/`: chat page (`/chat`, `/chat/<id>`, one route, so the page is not rebuilt).
- `src/app/core/api.ts`: the only way to reach the API (Tauri `api_request`).
- `src/app/core/theme.ts` + `src/styles.css`: dark/light tokens.
- `src-tauri/src/api.rs`: token reading and the loopback HTTP call.
- `src-tauri/capabilities/default.json`: the window can call only `api_request` and `api_health`.

`freezePrototype` is on in `tauri.conf.json`. A package that writes to
Object.prototype when it loads breaks the app silently; `npm run check:freeze`
catches that (it is why echarts is not used).

Opened in a plain browser (`ng serve`), the page loads but every request
fails with a note to open it in the app. This is on purpose.
