# Personal Assistant — Mac app

Angular 21 + Tauri 2 front end for the local assistant API. See the
"Mac app" section of the main `README.md` for how the token bridge works.

```bash
npm install
npx tauri dev                           # window, server on port 8000
ASSISTANT_API_PORT=8765 npx tauri dev   # another port
CI=1 npx ng test --watch=false          # Angular tests
(cd src-tauri && cargo test)            # bridge tests
```

Layout:

- `src/app/core/api.ts`: the only way to reach the API (Tauri `api_request`).
- `src/app/chat/`: chat page (`/chat`, `/chat/<id>`, one route, so the page is not rebuilt).
- `src-tauri/src/api.rs`: token reading and the loopback HTTP call.
- `src-tauri/capabilities/default.json`: the window can call only `api_request` and `api_health`.

Opened in a plain browser (`ng serve`), the page loads but every request
fails with a note to open it in the app. This is on purpose.
