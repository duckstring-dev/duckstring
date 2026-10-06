# Duckstring Docs

The documentation site, built with [Docusaurus](https://docusaurus.io/) and hosted at [docs.duckstring.com](https://docs.duckstring.com).

Content lives in `docs/` (this site is docs-only — pages route from `/`). The sidebar is defined explicitly in `sidebars.ts`. `docs/theory.md` is the authoritative orchestration spec; the other pages are written against the actual CLI/API surface in `src/duckstring/`, so check them when that surface changes.

```bash
npm install
npm start        # dev server with live reload
npm run build    # static build into build/ (fails on broken links)
```

## Blog

Posts live in `blog/` and are served at `duckstring.com/blog`. To write one, copy `blog/_template.md` to `blog/YYYY-MM-DD-short-name.md`. Each post must set an explicit `slug` (the build fails without one) so its URL survives the blog's eventual move to the commercial site, and must have a `<!-- truncate -->` marker below its opening. Authors are defined in `blog/authors.yml`. Posts with `draft: true` show in `npm start` but are left out of the build.
