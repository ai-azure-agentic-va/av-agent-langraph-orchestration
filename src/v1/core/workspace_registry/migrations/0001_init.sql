-- The workspace registry: one row per workspace (retired ones kept), its change
-- history, and the counter every write bumps.

CREATE TABLE workspace_registry.workspaces (
    alias text PRIMARY KEY,
    label text NOT NULL,
    host text NOT NULL,
    sql_warehouse_id text NOT NULL,
    data_allowlist text NULL,
    jobs_enabled boolean NOT NULL,
    groups text[] NOT NULL,
    position integer NOT NULL,
    retired_at timestamptz NULL,
    version integer NOT NULL,
    created_at timestamptz NOT NULL,
    created_by text NOT NULL,
    updated_at timestamptz NOT NULL,
    updated_by text NOT NULL
);

CREATE TABLE workspace_registry.history (
    id bigserial PRIMARY KEY,
    alias text NOT NULL,
    at timestamptz NOT NULL,
    actor_identity text NOT NULL,
    actor_name text NULL,
    actor_email text NULL,
    action text NOT NULL,
    before jsonb NULL,
    after jsonb NULL
);

CREATE INDEX history_alias_at ON workspace_registry.history (alias, at DESC);

-- Exactly one row: the primary key only admits true.
CREATE TABLE workspace_registry.registry_version (
    one_row boolean PRIMARY KEY DEFAULT true CHECK (one_row),
    version bigint NOT NULL
);

INSERT INTO workspace_registry.registry_version (version) VALUES (0);
