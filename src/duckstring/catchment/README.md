# Catchment

Start the production server (serves built Next.js files from `static/`):

```bash
uvicorn duckstring.catchment.app:app --reload
```

For development, run the Next.js dev server alongside FastAPI and set `NEXT_DEV_URL` to proxy frontend requests through FastAPI:

```bash
# Terminal 1
cd frontend && npm run dev

# Terminal 2
NEXT_DEV_URL=http://localhost:3000 uvicorn duckstring.catchment.app:app --reload
```

When `NEXT_DEV_URL` is set, FastAPI proxies all requests to the Next.js dev server instead of serving static files.