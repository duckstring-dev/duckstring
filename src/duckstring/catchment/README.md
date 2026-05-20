# Catchment

Start the production server (serves built Next.js files from `static/`):

```bash
uvicorn duckstring.catchment.app:app --reload
```

For development, run both servers and access the app on the Next.js port. The Next.js dev server proxies `/api/*` requests to FastAPI:

```bash
# Terminal 1
uvicorn duckstring.catchment.app:app --reload

# Terminal 2
cd frontend && npm run dev
```

Open `http://localhost:3000`. API calls to `/api/*` are forwarded to `http://localhost:8000` automatically. Set `FASTAPI_URL` in the frontend environment to override the backend URL.