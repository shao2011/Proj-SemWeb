# Frontend

Chat UI for the RAG API, styled after claude.ai dark mode. React 19 + TypeScript + Vite.

This is a harness for exercising the backend API by hand, not a product. There are
no frontend tests on purpose. The backend is where the test suite lives.

## Requirements

- Node 20 or newer (built and tested on 24.16)
- The backend running on port 8000

## Run it

Start Postgres and the API first, from the repo root. The api service waits for
the database and applies migrations before it serves:

```bash
docker compose up -d
curl http://localhost:8000/health
```

`configured_providers` must be non-empty for document search. If it comes back as `[]`, the
Projects screen will block project creation. Set `COHERE_API_KEY`, `VOYAGE_API_KEY` or
`JINA_API_KEY` in the root `.env`, then run `docker compose up -d` again so the container picks
up the new values. Editing `.env` alone does nothing to a container that is already running.

Chat needs a second key: `OPENROUTER_API_KEY`. Without it, sending a message returns 503.
`OPENROUTER_MODEL` selects the model and defaults to `openrouter/free`, OpenRouter's free
router, so chat costs nothing out of the box. Any slug from OpenRouter's model list works
(`anthropic/claude-opus-5`, `anthropic/claude-sonnet-5`, ...) once the account has credits.

Then start the UI:

```bash
npm install
npm run dev
```

Open http://localhost:5173.

## Scripts

| Command | What it does |
|---|---|
| `npm run dev` | Dev server on port 5173 with hot reload |
| `npm run build` | Typecheck, then build to `dist/` |
| `npm run preview` | Serve the built `dist/` locally |

## How it reaches the API

There is no CORS middleware on the backend, so the dev server proxies instead. Requests go to
`/api/*` and Vite forwards them to `http://localhost:8000` with the `/api` prefix stripped. The
target is set in `vite.config.ts`.

This only applies to `npm run dev`. A production build has no proxy, so you need a reverse proxy
in front of both, or CORS enabled on the API.

## Two kinds of chat

A thread either has a project or it does not, and that is fixed when the thread is created.

- **No project** — plain chat. Nothing is retrieved; the question plus prior turns go straight
  to the model. This is what you get when no project is selected.
- **With a project** — RAG. The question is embedded, searched, reranked, and the evidence is
  put in the prompt. Answers carry citations and a sources block.

Pick a project under Projects before starting the thread to get the second kind.

## Identity

Every request carries an `X-User-ID` header. Projects, documents, and conversations are all
private to that ID.

On first load the app generates a UUID and stores it in localStorage. Change it under Customize
in the sidebar. Switching IDs reloads everything for the new owner and deletes nothing.

Conversation history lives on the server, so it survives a reload and follows the user ID rather
than the browser. Only the user ID is kept locally.

## Layout

```
src/
  App.tsx              state, routing between views, send and upload flows
  lib/api.ts           typed client, X-User-ID injection, error mapping, SSE reader
  lib/storage.ts       localStorage for the user ID only
  lib/types.ts         mirrors the backend Pydantic models
  components/          Sidebar, Composer, Message, Panels, icons
  styles/global.css    the whole theme, no CSS framework
```

## Notes

- Upload accepts PDF, DOCX, XLSX and PPTX up to 25 MB. The backend also parses images, but the
  file picker does not offer them yet.
- Answers stream token by token. A failure part way through arrives as an SSE `error` event and
  renders in red; the question stays saved either way.
- The sources block under an answer is live only. Citation IDs are stored, the full hits are not,
  so reopening an old thread shows the answer without the expandable evidence.
