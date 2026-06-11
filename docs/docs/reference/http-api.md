---
title: HTTP API
description: The Catchment's REST surface.
---

# HTTP API Reference

:::caution Work in progress
This page is a stub. Content to be written in a later pass — document the routes in `src/duckstring/catchment/routes/`.
:::

**Planned content:**

- Deploy: pond upload/registration
- Orchestrate: triggers, control, `GET /api/status` (field-by-field), `GET /api/runs` (params: `pond`, `lineage`, `ripples`, `limit`), windows CRUD
- Data: querying exported Parquet
- Health
- Worker protocol (informational): `GET /api/duck/{pond}/jobs` short-poll and `POST /api/duck/{pond}/events`
- Status/state field semantics: `status` precedence (failed → killed → blocked → running → queued → idle), fault fields, freshness fields
