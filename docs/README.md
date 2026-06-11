# Duckstring Docs

The documentation site, built with [Docusaurus](https://docusaurus.io/) and hosted at [docs.duckstring.com](https://docs.duckstring.com).

Content lives in `docs/` (this site is docs-only — pages route from `/`). The sidebar is defined explicitly in `sidebars.ts`. `docs/theory.md` is the authoritative orchestration spec; most other pages are currently stubs with a "Planned content" outline, to be written out in a later pass.

```bash
npm install
npm start        # dev server with live reload
npm run build    # static build into build/ (fails on broken links)
```
