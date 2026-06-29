-- Access-level API keys. The single-key model is split into a total-ordered ladder
-- (read ⊂ demand ⊂ full) so an operator can hand a downstream a key that solicits demand and reads
-- without granting deploy/kill/delete. One row per level holds the SHA-256 *hash* of the key — the
-- plaintext is printed once at generation and never stored, so a leaked duck.db yields no usable key.
-- Rerolling a level replaces its hash in place (see catchment/auth.py, `duckstring catchment rotate-keys`).
CREATE TABLE catchment_key (
    level TEXT PRIMARY KEY,   -- 'read' | 'demand' | 'full'
    hash  TEXT NOT NULL       -- sha256 hex of the key
);
