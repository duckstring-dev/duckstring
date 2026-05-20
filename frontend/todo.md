# frontend/ — Next.js app

Run from this directory.

## Init

```bash
npx create-next-app@latest . --typescript --tailwind --eslint --app --src-dir --import-alias "@/*"
```

## Configure static export

In `next.config.js` (or `next.config.ts`), set the output mode and point the
dist directory at the Python package's static folder:

```js
/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "export",
  distDir: "../src/duckstring/catchment/static",
};

export default nextConfig;
```

This means `npm run build` writes directly into the location FastAPI serves from.
No copy step needed in CI.

## Dev workflow

```bash
npm install
npm run dev       # local Next.js dev server (hot reload, no FastAPI involved)
npm run build     # static export → ../src/duckstring/catchment/static
```

## CI pipeline sketch

```yaml
- run: npm ci
  working-directory: frontend
- run: npm run build
  working-directory: frontend
# static files are now in src/duckstring/catchment/static/
# proceed to build/publish the Python package or deploy the server
```
