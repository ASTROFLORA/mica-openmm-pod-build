"""
Product-layer schema migration for P0 tables.

Creates (IF NOT EXISTS) all product-layer tables in Neon PostgreSQL.
Idempotent — safe to run on every startup.

Tables:
  P0: studies, app_memories, workspace_snapshots, working_sets,
      working_set_items, artifacts, file_records
  P1: artifact_links, study_artifacts, search_intents

Usage:
  from mica.api_v1.product_schema import ensure_product_schema
  await ensure_product_schema()
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from mica.infrastructure.persistence.pg_async import (
    asyncpg_connection_kwargs_for_database_url,
    choose_neon_database_url,
    mask_dsn,
    validate_ident,
)

logger = logging.getLogger(__name__)

_P0_TABLES_SQL = """
-- LABORATORIES
CREATE TABLE IF NOT EXISTS laboratories (
    lab_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id TEXT NOT NULL,
    org_ref TEXT,
    slug TEXT NOT NULL,
    display_name TEXT NOT NULL,
    description TEXT,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    archived BOOLEAN NOT NULL DEFAULT false,
    UNIQUE(owner_user_id, slug)
);
CREATE INDEX IF NOT EXISTS idx_laboratories_owner ON laboratories(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_laboratories_org ON laboratories(org_ref);

-- LAB_MEMBERSHIPS
CREATE TABLE IF NOT EXISTS lab_memberships (
    membership_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    lab_id UUID NOT NULL REFERENCES laboratories(lab_id) ON DELETE CASCADE,
    principal_ref TEXT NOT NULL,
    role TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    invited_by TEXT,
    metadata JSONB DEFAULT '{}',
    joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(lab_id, principal_ref)
);
CREATE INDEX IF NOT EXISTS idx_lab_memberships_lab ON lab_memberships(lab_id);
CREATE INDEX IF NOT EXISTS idx_lab_memberships_principal ON lab_memberships(principal_ref);

-- KNOWLEDGE_SPACES
CREATE TABLE IF NOT EXISTS knowledge_spaces (
    space_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    lab_id UUID NOT NULL REFERENCES laboratories(lab_id) ON DELETE CASCADE,
    owner_user_id TEXT NOT NULL,
    slug TEXT NOT NULL,
    display_name TEXT NOT NULL,
    description TEXT,
    primary_parent_space_id UUID,
    review_cadence TEXT,
    health_status TEXT NOT NULL DEFAULT 'active',
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    archived BOOLEAN NOT NULL DEFAULT false,
    UNIQUE(lab_id, slug)
);
CREATE INDEX IF NOT EXISTS idx_knowledge_spaces_lab ON knowledge_spaces(lab_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_spaces_owner ON knowledge_spaces(owner_user_id);

-- KNOWLEDGE_MEMBERSHIPS
CREATE TABLE IF NOT EXISTS knowledge_memberships (
    membership_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    parent_space_id UUID NOT NULL REFERENCES knowledge_spaces(space_id) ON DELETE CASCADE,
    child_space_id UUID REFERENCES knowledge_spaces(space_id) ON DELETE CASCADE,
    member_kb_ref TEXT,
    relation_type TEXT NOT NULL,
    expansion_policy TEXT NOT NULL DEFAULT 'no_expand',
    primary_parent BOOLEAN NOT NULL DEFAULT false,
    metadata JSONB DEFAULT '{}',
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    archived BOOLEAN NOT NULL DEFAULT false,
    CHECK (
        ((child_space_id IS NOT NULL)::int + (member_kb_ref IS NOT NULL)::int) = 1
    )
);
CREATE INDEX IF NOT EXISTS idx_knowledge_memberships_parent ON knowledge_memberships(parent_space_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_memberships_child ON knowledge_memberships(child_space_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_memberships_kb_ref ON knowledge_memberships(member_kb_ref);

-- MEMBERSHIP_SNAPSHOTS
CREATE TABLE IF NOT EXISTS membership_snapshots (
    snapshot_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    space_id UUID NOT NULL REFERENCES knowledge_spaces(space_id) ON DELETE CASCADE,
    captured_by TEXT NOT NULL,
    snapshot_data JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_membership_snapshots_space ON membership_snapshots(space_id);

-- RESEARCH_LINES
CREATE TABLE IF NOT EXISTS research_lines (
    line_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    lab_id UUID NOT NULL REFERENCES laboratories(lab_id) ON DELETE CASCADE,
    owner_user_id TEXT NOT NULL,
    slug TEXT NOT NULL,
    display_name TEXT NOT NULL,
    description TEXT,
    primary_question TEXT,
    status TEXT NOT NULL DEFAULT 'proposed',
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    archived BOOLEAN NOT NULL DEFAULT false,
    UNIQUE(lab_id, slug)
);
CREATE INDEX IF NOT EXISTS idx_research_lines_lab ON research_lines(lab_id);
CREATE INDEX IF NOT EXISTS idx_research_lines_owner ON research_lines(owner_user_id);

-- RESEARCH_LINE_SPACE_LINKS
CREATE TABLE IF NOT EXISTS research_line_space_links (
    link_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    line_id UUID NOT NULL REFERENCES research_lines(line_id) ON DELETE CASCADE,
    space_id UUID NOT NULL REFERENCES knowledge_spaces(space_id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL DEFAULT 'related_domain',
    metadata JSONB DEFAULT '{}',
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(line_id, space_id, relation_type)
);
CREATE INDEX IF NOT EXISTS idx_research_line_space_links_line ON research_line_space_links(line_id);
CREATE INDEX IF NOT EXISTS idx_research_line_space_links_space ON research_line_space_links(space_id);

-- STUDIES
CREATE TABLE IF NOT EXISTS studies (
    study_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    lab_id UUID REFERENCES laboratories(lab_id) ON DELETE SET NULL,
    research_line_id UUID REFERENCES research_lines(line_id) ON DELETE SET NULL,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    tags TEXT[] DEFAULT '{}',
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    archived BOOLEAN NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS idx_studies_user ON studies(user_id);
CREATE INDEX IF NOT EXISTS idx_studies_created ON studies(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_studies_lab ON studies(lab_id);
CREATE INDEX IF NOT EXISTS idx_studies_research_line ON studies(research_line_id);

-- APP_MEMORIES
CREATE TABLE IF NOT EXISTS app_memories (
    memory_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT NOT NULL,
    study_id UUID,
    app_name TEXT NOT NULL,
    memory_data JSONB NOT NULL DEFAULT '{}',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(user_id, study_id, app_name)
);
CREATE INDEX IF NOT EXISTS idx_app_memories_user ON app_memories(user_id, app_name);

-- WORKSPACE_SNAPSHOTS
CREATE TABLE IF NOT EXISTS workspace_snapshots (
    snapshot_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    study_id UUID REFERENCES studies(study_id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    name TEXT,
    snapshot_data JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    restored_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_ws_snapshots_study ON workspace_snapshots(study_id);
CREATE INDEX IF NOT EXISTS idx_ws_snapshots_user ON workspace_snapshots(user_id);

-- WORKING_SETS (canonical product name; see SURFACE_COLLISION_AUDIT C-01)
CREATE TABLE IF NOT EXISTS working_sets (
    working_set_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    study_id UUID REFERENCES studies(study_id) ON DELETE SET NULL,
    user_id TEXT NOT NULL,
    title TEXT NOT NULL,
    purpose TEXT DEFAULT 'custom',
    description TEXT,
    layout_data JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_working_sets_user ON working_sets(user_id);
CREATE INDEX IF NOT EXISTS idx_working_sets_study ON working_sets(study_id);

-- WORKING_SET_ITEMS
CREATE TABLE IF NOT EXISTS working_set_items (
    item_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    working_set_id UUID NOT NULL REFERENCES working_sets(working_set_id) ON DELETE CASCADE,
    artifact_ref_type TEXT NOT NULL,
    artifact_ref_id TEXT NOT NULL,
    relation TEXT NOT NULL DEFAULT 'primary',
    position INTEGER NOT NULL DEFAULT 0,
    config JSONB DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_ws_items_set ON working_set_items(working_set_id);

-- ARTIFACTS
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT NOT NULL,
    artifact_type TEXT NOT NULL,
    display_name TEXT NOT NULL,
    source TEXT,
    gcs_key TEXT,
    content_hash TEXT,
    mime_type TEXT,
    size_bytes BIGINT,
    ref_url TEXT,
    job_id TEXT,
    lineage_id TEXT,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_artifacts_user ON artifacts(user_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_type ON artifacts(artifact_type);
-- Ensure columns that may be missing from earlier schema versions
ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS job_id TEXT;
ALTER TABLE artifacts ADD COLUMN IF NOT EXISTS lineage_id TEXT;
CREATE INDEX IF NOT EXISTS idx_artifacts_job ON artifacts(job_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_lineage ON artifacts(lineage_id);
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_knowledge_spaces_primary_parent'
    ) THEN
        ALTER TABLE knowledge_spaces
            ADD CONSTRAINT fk_knowledge_spaces_primary_parent
            FOREIGN KEY (primary_parent_space_id) REFERENCES knowledge_spaces(space_id) ON DELETE SET NULL;
    END IF;
END $$;
ALTER TABLE studies ADD COLUMN IF NOT EXISTS lab_id UUID REFERENCES laboratories(lab_id) ON DELETE SET NULL;
ALTER TABLE studies ADD COLUMN IF NOT EXISTS research_line_id UUID REFERENCES research_lines(line_id) ON DELETE SET NULL;

-- PERMISSION_POLICIES (P0 — blocking for collaboration)
CREATE TABLE IF NOT EXISTS permission_policies (
    policy_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    access_level TEXT NOT NULL DEFAULT 'owned',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(resource_type, resource_id)
);
CREATE INDEX IF NOT EXISTS idx_permission_policies_resource ON permission_policies(resource_type, resource_id);

-- PERMISSION_ENTRIES (P0 — ACL for shared resources)
CREATE TABLE IF NOT EXISTS permission_entries (
    entry_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    policy_id UUID NOT NULL REFERENCES permission_policies(policy_id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'viewer',
    granted_by TEXT NOT NULL,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(policy_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_permission_entries_user ON permission_entries(user_id);

-- ARTIFACT_LINEAGE (P1 — causal provenance, stronger than artifact_links)
CREATE TABLE IF NOT EXISTS artifact_lineage (
    lineage_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    artifact_id UUID NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
    source_artifact_id UUID REFERENCES artifacts(artifact_id) ON DELETE SET NULL,
    source_job_id TEXT,
    source_protocol_run_id TEXT,
    source_receipt_ref TEXT,
    lineage_type TEXT NOT NULL,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_artifact_lineage_artifact ON artifact_lineage(artifact_id);
CREATE INDEX IF NOT EXISTS idx_artifact_lineage_source ON artifact_lineage(source_artifact_id);

-- JOB_RECEIPTS (P0 — durable receipt storage, separate from compute)
CREATE TABLE IF NOT EXISTS job_receipts (
    receipt_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    outputs JSONB DEFAULT '[]',
    cost_estimate_usd NUMERIC(10,6),
    cost_actual_usd NUMERIC(10,6),
    duration_seconds INTEGER,
    provider TEXT,
    status TEXT NOT NULL DEFAULT 'completed',
    provenance_refs TEXT[] DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_job_receipts_job ON job_receipts(job_id);
CREATE INDEX IF NOT EXISTS idx_job_receipts_user ON job_receipts(user_id);

-- ARTIFACT_PREVIEWS (P1 — cheap thumbnails/previews separate from full artifact)
CREATE TABLE IF NOT EXISTS artifact_previews (
    preview_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    artifact_id UUID NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
    gcs_key TEXT NOT NULL,
    mime_type TEXT,
    size_bytes BIGINT,
    content_hash TEXT,
    preview_not_canonical BOOLEAN NOT NULL DEFAULT true,
    source_receipt_ref TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_artifact_previews_artifact ON artifact_previews(artifact_id);

-- SEARCH_INTENTS (P1 — must come BEFORE search_intent_results)
CREATE TABLE IF NOT EXISTS search_intents (
    intent_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT NOT NULL,
    query_text TEXT NOT NULL,
    intent_type TEXT NOT NULL DEFAULT 'free_text',
    filters JSONB DEFAULT '{}',
    desired_output TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    result_count INTEGER,
    result_snapshot JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_search_intents_user ON search_intents(user_id, created_at DESC);

-- SEARCH_INTENT_RESULTS (P1 — paginated, auditable search results)
CREATE TABLE IF NOT EXISTS search_intent_results (
    result_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    intent_id UUID NOT NULL REFERENCES search_intents(intent_id) ON DELETE CASCADE,
    source_lane TEXT,
    rank INTEGER,
    score DOUBLE PRECISION,
    artifact_id UUID REFERENCES artifacts(artifact_id) ON DELETE SET NULL,
    provider TEXT,
    result_payload JSONB DEFAULT '{}',
    can_ingest BOOLEAN DEFAULT false,
    indexed_in_kb BOOLEAN DEFAULT false,
    retrieval_trace_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_search_intent_results_intent ON search_intent_results(intent_id);

-- FILE_RECORDS
CREATE TABLE IF NOT EXISTS file_records (
    file_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    display_name TEXT,
    file_type TEXT NOT NULL,
    gcs_key TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    mime_type TEXT,
    size_bytes BIGINT NOT NULL DEFAULT 0,
    sync_status TEXT NOT NULL DEFAULT 'cloud_only',
    local_path TEXT,
    tags TEXT[] DEFAULT '{}',
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(user_id, gcs_key)
);
CREATE INDEX IF NOT EXISTS idx_file_records_user ON file_records(user_id);
CREATE INDEX IF NOT EXISTS idx_file_records_hash ON file_records(content_hash);
CREATE INDEX IF NOT EXISTS idx_file_records_type ON file_records(file_type);

-- ARTIFACT_LINKS (P1)
CREATE TABLE IF NOT EXISTS artifact_links (
    link_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_artifact_id UUID NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
    target_artifact_id UUID NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
    link_type TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(source_artifact_id, target_artifact_id, link_type)
);

-- STUDY_ARTIFACTS (P1)
CREATE TABLE IF NOT EXISTS study_artifacts (
    study_id UUID NOT NULL REFERENCES studies(study_id) ON DELETE CASCADE,
    artifact_id UUID NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
    added_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(study_id, artifact_id)
);

-- ARTIFACT_MEMBERSHIPS (APV-04) — typed container attachment + origin lineage
CREATE TABLE IF NOT EXISTS artifact_memberships (
    membership_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    artifact_id UUID NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
    container_type TEXT NOT NULL CHECK (container_type IN ('study','knowledge_space','workspace','research_line')),
    container_id TEXT NOT NULL,
    home_scope_id TEXT NOT NULL,
    semantic_role TEXT NOT NULL DEFAULT 'attached',
    origin_membership_id UUID REFERENCES artifact_memberships(membership_id) ON DELETE SET NULL,
    attached_by TEXT NOT NULL,
    grantee_principal_ref TEXT,
    acl_role TEXT NOT NULL DEFAULT 'viewer',
    receipt_id TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    archived BOOLEAN NOT NULL DEFAULT false,
    UNIQUE(artifact_id, container_type, container_id)
);
CREATE INDEX IF NOT EXISTS idx_artifact_memberships_container
    ON artifact_memberships(container_type, container_id);
CREATE INDEX IF NOT EXISTS idx_artifact_memberships_artifact
    ON artifact_memberships(artifact_id);
CREATE INDEX IF NOT EXISTS idx_artifact_memberships_grantee
    ON artifact_memberships(grantee_principal_ref)
    WHERE grantee_principal_ref IS NOT NULL;

-- FINDINGS + EVIDENCE_BINDINGS (APV-05)
CREATE TABLE IF NOT EXISTS findings (
    finding_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    home_scope_id TEXT NOT NULL,
    statement TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft','promoted','retracted')),
    created_by TEXT NOT NULL,
    receipt_id TEXT NOT NULL,
    promoted_at TIMESTAMPTZ,
    promotion_receipt_id TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS evidence_bindings (
    binding_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    finding_id UUID NOT NULL REFERENCES findings(finding_id) ON DELETE CASCADE,
    artifact_id UUID NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
    artifact_membership_id UUID NOT NULL REFERENCES artifact_memberships(membership_id) ON DELETE RESTRICT,
    evidence_path_id TEXT,
    semantic_role TEXT NOT NULL
        CHECK (semantic_role IN ('supports','contradicts','context','method','result')),
    excerpt_selector JSONB,
    created_by TEXT NOT NULL,
    receipt_id TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_evidence_bindings_finding ON evidence_bindings(finding_id);
CREATE INDEX IF NOT EXISTS idx_evidence_bindings_membership ON evidence_bindings(artifact_membership_id);
CREATE INDEX IF NOT EXISTS idx_evidence_bindings_path
    ON evidence_bindings(evidence_path_id)
    WHERE evidence_path_id IS NOT NULL;

-- EXPERIENCE WORKSPACES (APV-06 typed composition; refs only)
CREATE TABLE IF NOT EXISTS experience_workspaces (
    workspace_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_kind TEXT NOT NULL CHECK (workspace_kind IN ('study','ad_hoc')),
    home_scope_id TEXT NOT NULL,
    root_study_id TEXT,
    created_by TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT 'Workspace',
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (
        (workspace_kind = 'study' AND root_study_id IS NOT NULL)
        OR (workspace_kind = 'ad_hoc' AND root_study_id IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS experience_working_sets (
    working_set_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES experience_workspaces(workspace_id) ON DELETE CASCADE,
    home_scope_id TEXT NOT NULL,
    name TEXT NOT NULL,
    root_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
    object_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_by TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS experience_workspace_views (
    view_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    working_set_id UUID NOT NULL REFERENCES experience_working_sets(working_set_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    layout_mode TEXT NOT NULL DEFAULT 'semantic',
    surface_binding_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    filter_spec JSONB NOT NULL DEFAULT '{}'::jsonb,
    grouping_spec JSONB NOT NULL DEFAULT '{}'::jsonb,
    layout_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- PRODUCT EVENT OUTBOX (APV-07) — durable projection optional; process store is live authority
CREATE TABLE IF NOT EXISTS product_event_outbox (
    event_id TEXT PRIMARY KEY,
    schema_urn TEXT NOT NULL DEFAULT 'urn:mica:event:product:v1',
    event_type TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    session_id TEXT NOT NULL,
    actor_user_id TEXT NOT NULL,
    effective_scope_id TEXT NOT NULL,
    subject_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
    correlation_id TEXT NOT NULL,
    causation_id TEXT,
    receipt_ref TEXT,
    replay_cursor TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    completeness TEXT NOT NULL DEFAULT 'terminal'
        CHECK (completeness IN ('partial','terminal')),
    UNIQUE(session_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_product_event_outbox_session_seq
    ON product_event_outbox(session_id, sequence);

"""


_POOL: Optional[object] = None


async def _get_neon_pool():
    """Get or create a Neon connection pool (dedicated for product schema ops)."""
    global _POOL
    if _POOL is not None:
        return _POOL

    dsn = choose_neon_database_url()
    if not dsn:
        raise RuntimeError("NEON_DATABASE_URL is not configured")

    try:
        import asyncpg
    except ImportError:
        raise RuntimeError("asyncpg is required for product schema")

    kwargs = asyncpg_connection_kwargs_for_database_url(dsn)
    kwargs.setdefault("min_size", 1)
    kwargs.setdefault("max_size", 5)
    _POOL = await asyncpg.create_pool(dsn, **kwargs)
    logger.info("Product schema pool created (Neon: %s)", mask_dsn(dsn))
    return _POOL


async def ensure_product_schema() -> bool:
    """Create all product-layer tables if they don't exist. Returns True on success."""
    try:
        pool = await _get_neon_pool()
    except Exception as exc:
        logger.warning("Product schema skipped — Neon unavailable: %s", exc)
        return False

    async with pool.acquire() as conn:
        await conn.execute(_P0_TABLES_SQL)

    logger.info("Product schema ensured (studies, app_memories, snapshots, window_groups, artifacts, file_records, search_intents)")
    return True
