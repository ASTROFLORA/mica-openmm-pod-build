# MCP_STANDARDIZATION.md

**Status**: PARTIALLY IMPLEMENTED — local closure code landed on 2026-04-22; production endpoint still trails pending deploy.
**Date**: 2026-04-21 (updated 2026-04-22)
**Scope**: Migrate the Driver↔Daemon Ouroboros loop (validated in Crucible v4/v5) from its current bespoke GCS-polling protocol to the **Model Context Protocol (MCP)** standard, so that any MCP-capable client (GitHub Copilot, Claude Desktop, Cursor, Aider, custom CLIs) can drive the remote `AgenticDriver` hosted on Railway and consume its tool surface with zero bespoke glue.
**Non-goals**: Replacing the FastAPI HTTP/WS router; removing the AgenticDriver; changing tool semantics. MCP is an **adapter layer**, not a rewrite.

---

## 0. TL;DR

Today we have:

```
 Local repo ──(watchdog upload)──► gs://mica-user-XXXX/workspace_snapshots/latest/
                                                                 │
                                                                 ▼
                                              Railway AgenticDriver reads files
                                                                 │
                                                                 ▼
 Local daemon polls  ◄──(JSON directive)── gs://.../directives/YYYY/MM/DD/*.json
```

We want:

```
 MCP client  ◄──► [mica-mcp-local]  ◄──(GCS transport, JSON-RPC 2.0 envelopes)──►  [mica-mcp-remote / Railway]
     ▲                                                                                     │
     │                                                                                     ▼
     └─ GHP slash command (/mica.*) ──────────────────────────────────► AgenticDriver tools
```

Both sides speak **MCP over a GCS-bucket transport**. The current daemon becomes a tiny MCP **stdio server** that GHP / Copilot / any MCP client mounts. The Railway side exposes an MCP **endpoint server** that wraps the existing `AgenticDriver` without touching its internals.

---

## 1. Why MCP

The Ouroboros loop already has the three MCP primitives in disguise:

| Today (bespoke)                                  | MCP primitive  |
| ------------------------------------------------ | -------------- |
| `workspace_snapshots/latest/*` read by driver    | **Resource**   |
| `publish_operator_directive`, `run_deep_research`, `search_literature`, `run_dlm_scan`, `run_bibliotecario_scan`, `list_workspace_files`, `read_workspace_file_content` | **Tool**       |
| Driver system prompt + ODRC directive templates  | **Prompt**     |

Adopting MCP gives us:

1. **Client interop** — any MCP-literate client (GHP, Claude Desktop, Cursor, Aider, Continue, LibreChat) can call MICA tools without forking `mica_agent.py`.
2. **Typed capability negotiation** — `capabilities/list` + `tools/list` replaces our `tool_capability_registry.py` *on the wire*, while keeping the registry as the source of truth server-side.
3. **Standard prompt injection** — the `prompts/get` flow lets the client ask MICA: "give me the ODRC refactor-directive prompt, filled with these arguments", instead of inlining a 100-line prompt in a shell script.
4. **Progress + streaming** — MCP has first-class `notifications/progress` and SSE-style streaming, which today we fake with `State thinking Step N` log lines.
5. **Authorization surface** — MCP supports per-tool allow-lists and OAuth2; we can expose `run_deep_research` to trusted clients only without bolting on another auth layer.

---

## 2. Capability Mapping

### 2.1 Resources

Resources are **read-only, URI-addressable** bytes. They map naturally onto our GCS surfaces.

| URI pattern                                                         | Maps to                                                                                       | Notes |
| ------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- | ----- |
| `mica://workspace/{relpath}`                                        | `gs://mica-user-{uid}/workspace_snapshots/latest/{relpath}`                                   | Read-only view of the watchdog-uploaded repo snapshot. Replaces `read_workspace_file_content` as a Resource (tools stay as compatibility shim). |
| `mica://workspace/`                                                 | Listing of above                                                                              | Replaces `list_workspace_files`. |
| `mica://directive/{directive_id}`                                   | `gs://.../directives/YYYY/MM/DD/{id}.json`                                                    | Each published ODRC directive becomes a resource the client can `resources/read`. |
| `mica://directive/{directive_id}/prompt`                            | `gs://.../prompts/YYYY/MM/DD/{id}.md`                                                         | The human-readable prompt Markdown. |
| `mica://directive/{directive_id}/response`                          | `gs://.../responses/YYYY/MM/DD/{id}.json`                                                     | Operator response (written by the local MCP server after execution). |
| `mica://kb/{session_id}/atoms`                                      | ATOM facts for a session                                                                      | |
| `mica://kb/{session_id}/scans/{scan_id}`                            | Document-scan artifacts                                                                       | Backed by `document_scans` / `document_scan_promotions` tables. |
| `mica://literature/{query_hash}/bundle`                             | Cached literature-artifact-bundle output                                                      | Lets clients `resources/read` a prior LRR without re-running. |
| `mica://feed/recent?limit=N`                                        | Live agent-feed tail                                                                          | Enables read-only visibility into the session bus. |

Resources are listed via `resources/list` with pagination (cursor = GCS `nextPageToken`). `resources/subscribe` uses the GCS transport's inbound-event channel (§3.3).

### 2.2 Tools

Tools are **side-effectful RPCs**. We map 1:1 from `tool_capability_registry.py`'s `_PUBLIC_TOOL_SPECS`. Proposed public MCP tool set (grouped by lane):

**LRR / Deep Research (the ones you specifically asked to exercise)**
- `search_literature` — unified lane (`semantic_scholar + pubmed + biorxiv + openalex`). Returns a bundle.
- `run_deep_research` — orchestrated multi-round corpus expansion with ATOM + citation graph.
- `run_dlm_scan` — Deep Literature Mining section-level scan.
- `run_bibliotecario_scan` — spawned specialist lane.
- `compile_research_briefing` — synthesized briefing over the bundle.
- `verify_citations` — Crossref + NCBI crosscheck.
- `query_co_occurrence`, `track_entity_evolution`, `analyse_citation_impact`, `analyse_knowledge_decay` — analytical lanes on top of the bundle.

**ODRC / Refactor orchestration**
- `publish_operator_directive` — writes directive JSON + prompt MD to GCS (unchanged semantics).
- `list_workspace_files`, `read_workspace_file_content` — kept as Tool aliases of the Resource reads for clients that don't speak `resources/read`.

**Workspace / KB**
- `add_to_workspace`, `list_workspace_sessions`, `list_workspace_assets`, `read_workspace_document`, `scan_workspace_document`, `scan_knowledge_base`, `promote_knowledge_base_scan`, `list_knowledge_base_atoms`, `download_pdf_to_workspace`.

**Structure / Bio**
- `search_protein`, `resolve_pdb`, `analyze_structure`, `visualize_molecule`, `load_knowledge_graph`, `get_domain_coloring`, `generate_lmp`, `list_lmp_presets`, `enrich_protein_pharma`, `map_conformational_landscape` (placeholder-flagged).

**Vector / Retrieval**
- `milvus_hybrid_search`, `milvus_sequence_search`, `milvus_dct_search`, `milvus_stored_embedding_search`, `federated_retrieve`, `search_institutional_knowledge` (dev-only — gate behind a separate MCP client profile).

**Coordination / Feed**
- `scroll_agent_feed`, `publish_cue`, `open_session_signature`, `update_session_progress`, `feed_stats`, `feed_thread`.

Every tool's MCP `inputSchema` is generated from the existing pydantic models used by `mica_agent.py` + `ws_bridge.py`; its `annotations.readOnlyHint / destructiveHint / idempotentHint` is derived from the `ToolCapabilitySpec.capability_mode` field:

| `capability_mode`      | MCP annotations                                                |
| ---------------------- | -------------------------------------------------------------- |
| `offline-native`       | `readOnlyHint=true, idempotentHint=true`                       |
| `backend-native`       | `readOnlyHint=false, idempotentHint=false, destructiveHint=*`  |
| `network-native`       | `readOnlyHint=true, idempotentHint=false` (network-dependent)  |
| `sandbox-native`       | `destructiveHint=true, openWorldHint=true`                     |

### 2.3 Prompts

Prompts are **parameterized, server-authored prompt templates** the client can fetch and inject. We reclaim a lot of what `mica_agent.py` currently hardcodes.

| Prompt name                    | Arguments                                                     | Backing asset |
| ------------------------------ | ------------------------------------------------------------- | ------------- |
| `mica.odrc_refactor_directive` | `target_file`, `symbol`, `destination_module`, `rationale`    | The Crucible v5 prompt template, parameterized. |
| `mica.lrr_deep_research`       | `question`, `max_papers`, `depth`, `include_preprints`        | Replaces ad-hoc deep-research prompts. |
| `mica.bibliotecario_consult`   | `query`, `task`, `deep_synthesis` (bool)                      | Mirrors `consult_bibliotecario` MCP server from `mica-driver`. |
| `mica.briefing_compile`        | `topic`, `audience`, `format`                                 | |
| `mica.workspace_audit`         | `session_id`, `focus`                                         | |
| `mica.citation_verification`   | `doi_list` or `paper_bundle_uri`                              | |
| `mica.peer_review`             | `artifact_uri`, `standards=["nature","msrp"]`                 | |
| `mica.ouroboros_ignition`      | `objective`, `constraints`, `allowed_tools`                   | The v5-scrubbed ignition template, with heuristic-keyword guardrails baked in. |

Each prompt is fetched via `prompts/get`, returns an MCP `GetPromptResult` with a `messages[]` array. This is how GHP will inject MICA-authored prompts without any string plumbing in the extension.

---

## 3. GCS Transport for MCP

### 3.1 Why a custom transport

The stock MCP transports are **stdio** and **Streamable HTTP**. stdio requires the server to be a child process of the client (fine for the local side). Streamable HTTP requires a durable HTTP(S) endpoint — which we have (`mica-api-production.up.railway.app`), so the baseline choice is:

- **Local MCP server ↔ client**: stdio (standard, no invention).
- **Client ↔ Railway MCP server**: Streamable HTTP (standard, no invention).

So why a GCS transport at all? Because the **asynchronous, operator-in-the-loop** lane of MICA is a real need: the driver fires a long-running research/refactor job, the client may go offline, the operator materializes the directive minutes later, and the response flows back. Streamable HTTP is synchronous-with-SSE; it doesn't survive client disconnects. **GCS transport is the durable, offline-safe delivery channel** we already have built into the Ouroboros loop — we just re-shape it to carry JSON-RPC 2.0 framed messages instead of our ad-hoc directive shape.

### 3.2 Wire format

JSON-RPC 2.0 envelopes, one message per GCS object:

```
gs://mica-mcp-bus-{uid}/
  inbox/  {client → server messages, client writes}
    2026/04/21/1718ms-<msg_id>.req.json
  outbox/ {server → client messages, server writes}
    2026/04/21/1722ms-<msg_id>.res.json
  events/ {server-initiated notifications, server writes}
    2026/04/21/1723ms-<evt_id>.evt.json
  ack/    {client acks consumption (so server can GC)}
    2026/04/21/<msg_id>.ack
```

Each object payload:

```json
{
  "jsonrpc": "2.0",
  "id": "req-9b1c...",                        // absent for notifications
  "method": "tools/call",                      // or "resources/read", "prompts/get", ...
  "params": { "name": "run_deep_research", "arguments": { ... } },
  "mica": {
    "session_id": "ses-...",
    "user_id": "agent_cli",
    "trace_id": "trc-...",
    "issued_at": "2026-04-21T14:18:02.406Z",
    "correlation_id": "corr-..."               // ties req/res/progress events
  }
}
```

### 3.3 Delivery semantics

- **Server discovery**: client GETs `gs://mica-mcp-bus-{uid}/server.json` (static manifest) to get protocol version, supported methods, and the active Railway endpoint URL (for clients that prefer Streamable HTTP when online).
- **Polling vs pub/sub**: default polling interval τ = 2s (we'll dial down from the daemon's 15s). For production we can attach a **GCS Pub/Sub notification** on `outbox/` and `events/` and push to the client via a thin relay — eliminating polling entirely. This is a strict superset of what the daemon does today.
- **Ordering**: ID-keyed, not object-order-keyed. Clients maintain an in-flight map keyed by JSON-RPC `id`.
- **Backpressure**: `ack/` prefix lets both sides GC consumed messages; without acks for N minutes, the peer considers the other side offline and surfaces a `notifications/cancelled` to open requests.
- **Size limit**: single message ≤ 2 MB (GCS object soft cap for this bucket); oversized payloads (e.g. LRR bundles) return a resource URI the client can `resources/read` instead of inlining in the tool result.
- **Security**: bucket is per-user (already the case today). All messages are signed with an HMAC over `(jsonrpc, id, method, params-sha256, mica.issued_at)` using a secret stored in Railway `MICA_MCP_HMAC_KEY`; client ships its half via `MICA_MCP_HMAC_KEY` in the local env. HMAC check is enforced by both sides before dispatch.

### 3.4 Relationship to today's directive prefix

`directives/` and `prompts/` stay **exactly as they are**. They become a **compatibility view** on top of the MCP transport: every `tools/call` to `publish_operator_directive` still drops a directive JSON in `directives/YYYY/MM/DD/` for the daemon-that-no-longer-is. Old tooling (Aider watchers, manual inspection, forensic audits) keeps working.

---

## 4. MCP Server Structure (replacing the daemon)

Two servers, one protocol.

### 4.1 `mica-mcp-local` (replaces `tools/mica_local_daemon.py`)

- **Runtime**: Python 3.11 + `mcp` SDK. Ships stdio.
- **Process model**: launched on-demand by the MCP client (GHP, Claude Desktop, Cursor). No background `pythonw`, no startup batch script. Sleeps when no client is attached.
- **Responsibilities**:
  1. **Expose local filesystem as Resources**: `file:///<repo>/**/*` with the same `.railwayignore`-derived exclude list we already use. Replaces the watchdog+debounce+upload loop for *client*-initiated reads.
  2. **Afferent pump**: keeps the existing debounced watchdog upload to `workspace_snapshots/latest/` so the remote driver can still read the repo even when no MCP client is attached. This is a daemon thread inside `mica-mcp-local`, lifecycle tied to server lifetime.
  3. **Efferent pump**: subscribes to `mica://directive/*` resources on the GCS transport. New directives arriving from Railway are surfaced as `notifications/resources/list_changed`; clients can then `resources/read` them and decide what to do (materialize to `.mica/directive_queue/`, feed to Aider, render in the chat pane, etc.).
  4. **Proxy tools**: any MCP tool call the client issues is forwarded to `mica-mcp-remote` via GCS transport (async) or Streamable HTTP (sync when online). Response is returned to the client.
  5. **Operator execution**: optional `mica.operator_execute` tool that, when called, materializes a directive's prompt to `.mica/directive_queue/{id}.md` and (if configured) runs `aider --message <prompt>`. This is the only place the current daemon's `MICA_DAEMON_EXEC_CMD` lives.

Config file: `.mica/mcp_local.toml`.

### 4.2 `mica-mcp-remote` (new, lives on Railway)

- **Runtime**: FastAPI + `mcp` SDK Streamable HTTP transport **OR** a thin worker that serves the GCS transport. Both can be enabled.
- **Integration point**: wraps `AgenticDriver` as an MCP **server backend**. Every `tools/call` becomes an `AgenticDriver._dispatch_tool(name, args)` invocation; every `resources/read` hits `gcs_user_storage`. No change to `agentic_driver.py`.
- **Tool registration**: built from `TOOL_CAPABILITY_REGISTRY`. Filtered per client profile (§7). The `HARD RULE` that blocks `publish_operator_directive` without prior `read_workspace_file_content` evidence moves into the MCP server's per-session state.
- **Prompt registration**: list in §2.3.
- **Deployment**: same Railway service (`mica-api-production`), mounted at `/mcp` using Streamable HTTP. The GCS-transport worker runs inside `mica-worker` (future — when split per kernel architecture).

### 4.3 What the current daemon loses

- Its own `processed_directives.json` dedup file (MCP ids + acks replace it).
- Its polling loop (replaced by Pub/Sub push + client-requested `resources/list`).
- Its `execute_directive` injection logic (moves to the `mica.operator_execute` tool, opt-in).

---

## 5. GHP (GitHub Copilot) Integration — "commands"

What you want is: **slash commands in GHP that invoke the remote driver from my editor**. The clean, standard way to do this in 2026:

1. Register `mica-mcp-local` as an **MCP server** in VS Code (`settings.json`):

   ```jsonc
   "mcp": {
     "servers": {
       "mica": {
         "command": "C:\\Users\\busta\\Downloads\\MICA\\.venv\\Scripts\\python.exe",
         "args": ["-m", "mica_mcp_local", "--config", "${workspaceFolder}/.mica/mcp_local.toml"],
         "env": {
           "MICA_USER_ID": "agent_cli",
           "MICA_BACKEND": "https://mica-api-production.up.railway.app"
         }
       }
     }
   }
   ```

2. Copilot Chat auto-discovers the MCP server's **tools** and **prompts**. No custom extension code. Tools show up as `#mica.search_literature`, `#mica.run_deep_research`, etc. Prompts show up as `/mica.odrc_refactor_directive`, `/mica.lrr_deep_research`, etc.

3. Plus a tiny repo-local **prompt file** layer (VS Code `.github/prompts/*.prompt.md`) that wraps the MCP prompts with opinionated defaults so you type one short slash command and the agent does the right thing.

### 5.1 Proposed slash commands (repo prompt files)

Drop these in `.github/prompts/` — VS Code Copilot Chat will expose them as `/mica-*`:

| Slash command          | What it does                                                                                      | Backing MCP call |
| ---------------------- | ------------------------------------------------------------------------------------------------- | ---------------- |
| `/mica-lrr`            | Runs LRR on the chat's current question; returns a bundle URI and a briefing.                     | `tools/call run_deep_research` → `tools/call compile_research_briefing` |
| `/mica-deep-research`  | Like `/mica-lrr` but with `depth=deep`, bibliotecario enabled, entity-evolution tracked.          | `tools/call run_deep_research(depth="deep", bibliotecario=true)` |
| `/mica-dlm`            | Section-level deep literature mining on a topic.                                                  | `tools/call run_dlm_scan` |
| `/mica-biblio`         | Spawns the Bibliotecario specialist for a targeted synthesis.                                     | `tools/call run_bibliotecario_scan` |
| `/mica-refactor`       | Forensic read of a target file/symbol → ODRC directive in GCS.                                    | `prompts/get mica.odrc_refactor_directive` → `tools/call publish_operator_directive` |
| `/mica-ouroboros`      | Full ignition cycle: read → analyze → directive → local materialization.                          | Composite chain, driver-side. |
| `/mica-briefing`       | Compile a briefing over a prior LRR bundle URI.                                                   | `tools/call compile_research_briefing` |
| `/mica-verify`         | Citation verification over a DOI list or bundle URI.                                              | `tools/call verify_citations` |
| `/mica-audit`          | Workspace audit — what KB atoms, sessions, assets exist for a session.                            | `resources/list mica://workspace/`, `tools/call list_workspace_sessions` |
| `/mica-feed`           | Tail of the live agent feed, most recent N posts.                                                 | `resources/read mica://feed/recent?limit=50` |
| `/mica-peer-review`    | Peer-reviews a given artifact (LRR bundle, briefing, spec) against MSRP/Nature standards.         | `tools/call request_peer_review` |
| `/mica-status`         | Remote driver health + capability snapshot.                                                       | `tools/call feed_stats` + MCP `tools/list`. |

Each `.prompt.md` file is ~15-30 lines: metadata frontmatter + the MCP prompt name + placeholders for the Copilot chat context. The actual content lives on the server.

### 5.2 Example: `.github/prompts/mica-lrr.prompt.md`

```yaml
---
mode: agent
description: Run MICA's Literature Retrieval & Reasoning lane on a question from the current chat.
tools: [mica.run_deep_research, mica.compile_research_briefing, mica.verify_citations]
---
Use MICA's MCP server to answer the user's question below with a citation-grounded briefing.

Steps:
1. Call `mica.run_deep_research` with `question=<user question>`, `max_papers=80`, `depth="standard"`.
2. Take the returned bundle URI and call `mica.compile_research_briefing` with `topic=<user question>`, `audience="senior scientist"`, `format="markdown"`.
3. Call `mica.verify_citations` on the briefing's DOI list.
4. Present the briefing inline with a trailing "Citation integrity: X/Y verified".

User question: ${input}
```

---

## 6. Elegant Dev Workflow

The whole point is you should stop hand-editing `_crucible_vN_prompt.txt` files. Here's the elegant standard shape:

```
┌──────────────────────────────────────────────────────────────┐
│ Dev types in GHP:    /mica-refactor target=agentic_driver.py │
│                                    symbol=_build_loop_executor│
├──────────────────────────────────────────────────────────────┤
│ GHP loads prompt:    mica.odrc_refactor_directive            │
│                      (server-authored, always up-to-date)    │
├──────────────────────────────────────────────────────────────┤
│ GHP runs agent loop: read_workspace_file_content             │
│                      → publish_operator_directive            │
├──────────────────────────────────────────────────────────────┤
│ mica-mcp-local sees: notifications/resources/list_changed    │
│                      on mica://directive/{id}                │
├──────────────────────────────────────────────────────────────┤
│ Operator tool fires: .mica/directive_queue/{id}.md           │
│                      (+ aider --message if configured)       │
└──────────────────────────────────────────────────────────────┘
```

No prompt-file authorship, no heuristic-keyword scrubbing (the heuristic gets removed or MCP-side filtered), no subprocess plumbing. The only thing the developer types is the slash command.

---

## 7. Client Profiles & Authorization

MCP supports per-client tool filtering. We use it to split:

| Profile        | Tools exposed                                                                     | Gate |
| -------------- | --------------------------------------------------------------------------------- | ---- |
| `dev`          | All public tools + spawn tools + `search_mica_institutional_memory` + sandbox     | local HMAC + repo presence |
| `operator`     | All public tools except sandbox and institutional-memory                          | HMAC |
| `readonly`     | Resources + `search_literature`, `compile_research_briefing`, `feed_stats`, `scroll_agent_feed` | HMAC |
| `ci`           | `list_workspace_files`, `read_workspace_file_content`, `run_deep_research` (rate-limited) | CI token |

Each client profile lives in `.mica/mcp_profiles/*.toml` on Railway, hot-reloadable.

---

## 8. Migration Plan (no code yet — just the slices)

1. **Slice 0 (docs)** — this file.
2. **Slice 1 (capability schema)** — auto-generate MCP `tools.json` and `resources.json` manifests from `tool_capability_registry.py` and commit them to `src/mica/mcp/manifests/`.
3. **Slice 2 (remote server)** — mount `mica-mcp-remote` at `POST /mcp` on Railway, Streamable HTTP. Internal dispatch to `AgenticDriver._dispatch_tool` (new thin method; already latent in the driver). Ship with 5 tools only: `list_workspace_files`, `read_workspace_file_content`, `publish_operator_directive`, `run_deep_research`, `search_literature`. Validate with MCP Inspector.
4. **Slice 3 (GCS transport)** — implement the JSON-RPC-over-GCS framing. Dual-write: every tool call also lands in `directives/` so the existing daemon keeps working.
5. **Slice 4 (local server)** — build `mica-mcp-local` as a stdio MCP server that wraps the GCS transport. Mount in VS Code via `settings.json`.
6. **Slice 5 (prompts)** — port the v5 ignition template and the ODRC refactor template into server-side `prompts/get` handlers.
7. **Slice 6 (slash commands)** — add the `.github/prompts/*.prompt.md` files from §5.1.
8. **Slice 7 (daemon retirement)** — flip `mica_local_daemon.py` into a stub that warns "deprecated, use mica-mcp-local"; keep the watchdog-upload thread running inside `mica-mcp-local` as a daemon thread.
9. **Slice 8 (tool fan-out)** — expose the rest of `_PUBLIC_TOOL_SPECS` under MCP, tier by tier, each behind its client profile.
10. **Slice 9 (Pub/Sub)** — replace polling with Cloud Pub/Sub notifications on the bus bucket.
11. **Slice 10 (peer-review)** — turn `request_peer_review` into a fully MCP-driven loop so the standard commands you use here become cross-client.

Each slice is independently shippable, each has a rollback, each leaves the bespoke Ouroboros path working.

---

## 9. Open Questions

- **Streaming progress**: MCP has `notifications/progress`. For `run_deep_research` (30-120s) we must emit per-round progress or clients will time out. Decision: every `AgenticDriver` step log (`State thinking Step N`) becomes a progress notification with `progress/total`.
- **Cost tracking**: where does `costUsd` land on the wire? Proposal: `_meta.mica.cost` on the `CallToolResult` envelope.
- **Session binding**: MCP sessions aren't MICA sessions. We'll carry `mica.session_id` in `_meta` and mint one if absent.
- **Heuristic removal**: the `_looks_like_scientific_workflow` trap (§3 of the Crucible v4 analysis) should be deleted once MCP-dispatched tools route directly — MCP requests are explicit tool calls, not natural-language heuristics.
- **Multi-tenant transport bucket**: `mica-mcp-bus-{uid}` per user means per-user MCP isolation. For team/workspace use, introduce `mica-mcp-bus-team-{team_id}` with shared HMAC rotation.

---

## 10. Acceptance Criteria

This blueprint is successful if, after Slices 1-6:

- A dev types `/mica-deep-research what is known about OSR1/SPAK phosphoregulation` in GHP and gets a citation-grounded briefing without a single bespoke script.
- A dev types `/mica-refactor target=<file> symbol=<fn>` in GHP and the local `.mica/directive_queue/` materializes a valid ODRC directive within ≤15s of the remote `publish_operator_directive` call.
- Claude Desktop, Cursor, and Aider — configured with the same `mica-mcp-local` stdio server — exhibit the same behavior, zero code changes.
- The existing daemon is retired and forensic auditors can still read `directives/YYYY/MM/DD/` exactly as before.

---

*End of blueprint. Next step on your signal: Slice 1 (manifest generator), or a deeper X-ray of a specific tool's MCP shape (e.g. `run_deep_research`'s full `inputSchema` / progress notification shape).*
