# MICA Multi-Agent Conversation Architecture
## Roadmap v1.0 — March 2026

---

## The Problem: Context Contamination

Current state (bad):
```
[DRIVER CONTEXT — 128k tokens]
  user: "Explica autoinhibición WNK1"
  tool_result(search_literature): {87 papers × 400char abstracts = 34k tokens}  ← WRONG
  assistant: "..."
  tool_result(search_protein): {3 UniProt entries = 2k tokens}
  assistant: "..."
  ...
```
The driver LLM reads 87 abstracts to decide what to say next.
That's not how a PI works. That's how a grad student drowns.

---

## The Model: Isolated Context Agents

```
┌─ MAIN STREAM (visible to user) ─────────────────────────────────────────────┐
│                                                                               │
│  🧠 DRIVER   "Necesito entender mecanismo de autoinhibición WNK1"            │
│                                                                               │
│  🔬 BIBLIOTECARIO  [leyendo 87 papers — contexto propio, aislado]            │
│     thinking... step 1                                                        │
│     → cite_finding(paperId="Murthy2022", finding="PF1 pseudosubstrate...")   │
│     → identify_gap(gap_type="contradiction", description="Zagórska vs...")   │
│     "Los papers 2019-2024 convergen en PF1. CRÍTICO: Murthy 2022 cambia      │
│      el modelo — el loop C-term es condicional a [Mg2+], no constitutivo.    │
│      Gap experimental confirmado. Recomiendo verificar 4BWJ vs K233E."       │
│                                                                               │
│  🧠 DRIVER   "¿K233E desestabiliza el loop de activación?"                   │
│                                                                               │
│  ⚗️  BIODYNAMO  [probando conformación — contexto propio]                    │
│     speaking... "RMSD 2.8Å en loop activación con K233E. Confirma          │
│      desestabilización. DCD disponible."                                      │
│                                                                               │
│  📋 MSRP_REVIEWER  [critique del driver — contexto propio]                   │
│     "SKEPTICISM: falta réplica estadística (N=1 simulación).                 │
│      EVIDENCE DEMAND: necesitas datos experimentales Kd para validar."       │
│                                                                               │
│  🧠 DRIVER   "Síntesis: mecanismo PF1 + evidencia MD K233E + gaps abiertos" │
│                                                                               │
├─ SIDE DATA (nunca entra en contexto LLM, va al panel UI de Alejandria) ──────┤
│  📑 PANEL research:   87 papers con índice escaneable                        │
│  🏗️  PANEL structure: DCD K233E / PDB diff 4BWJ                              │
│  📄 PANEL pdf:        Murthy 2022 full text (jump to section 3, p.12)        │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## Architecture

### 3 Canales de Salida

| Canal | Entra al LLM del driver | Va a UI | Propósito |
|-------|------------------------|---------|-----------|
| **Context stream** | ✅ Solo síntesis ~800 tokens | - | El driver decide qué hacer |
| **Agent conversation stream** | ❌ | ✅ Visible al usuario | El usuario ve el diálogo |
| **Side data** | ❌ Nunca | ✅ Panel lateral | papers/PDFs/DCD/estructuras navigables |

### Event Types (events.py)

```
AnyLoopEvent
├── StreamStart, TextDelta, ToolCallStart, ToolCallEnd  ← driver inference
├── StepFinish, LoopFinish, RetryWait, Error            ← lifecycle
├── ContextCompacted, ContextOverflow, ResourceInjected ← context management
├── AgentTurn    ← NEW: sub-agent dialogue visible al usuario
│   │  agent: "bibliotecario" | "biodynamo" | "alchemist" | "msrp_reviewer"
│   │  role:  "thinking" | "speaking" | "tool" | "done"
│   │  text:  content
│   └── session_id: sub-loop ID (ephemeral)
└── SideData     ← NEW: artifacts to UI side panel, never to LLM context
    │  channel: "research" | "structure" | "trajectory" | "pdf"
    │  agent:   who produced it
    └── payload: Dict (papers[], pdb_path, dcd_path, etc.)
```

### Execution Flow

```python
# run_streaming() in AgenticDriver
_pending: List[AnyLoopEvent] = []
executor = self._build_loop_executor(user_id, pending=_pending)
all_tools = MICA_TOOLS + _SPAWN_TOOLS  # adds consult_* tools

async for event in AgenticLoop.run(tools=all_tools, tool_executor=executor):
    if isinstance(event, ToolCallEnd):
        while _pending:            # drain sub-agent events BEFORE ToolCallEnd
            yield _pending.pop(0)  # → AgentTurn, SideData appear in stream
    yield event                    # → normal driver events
```

### Sub-Agent Lifecycle (spawn_agent)

```
Tool call: consult_bibliotecario(query="WNK1 autoinhibition", task="identify key mechanism")
  │
  ├─ Fetch 40 papers (BatchLiteratureFetcher) → pushed to SideData side panel
  │
  ├─ spawn_agent("bibliotecario"):
  │   AgenticLoop ephemeral (max_iterations=8, temperature=0.3)
  │   Context: corpus_msg (40 papers × 350 chars = ~15k tokens) ← stays HERE
  │   Tools: cite_finding, identify_gap
  │   Events → wrapped as AgentTurn → pushed to _pending
  │   Loop ends → context destroyed
  │
  └─ Returns synthesis (~600 tokens) → ONLY this enters driver context
```

---

## Agents Roster

| Agent | Triggered by | Context budget | Output |
|-------|-------------|---------------|--------|
| **bibliotecario** | `consult_bibliotecario` | 30-40 papers + task | ~600 token synthesis |
| **biodynamo** | `consult_specialist(specialist="biodynamo")` | PDB + task | MD result + DCD ref |
| **alchemist** | `consult_specialist(specialist="alchemist")` | molecule + task | ADME/docking result |
| **smic** | `consult_specialist(specialist="smic")` | graph + task | cavity/topology result |
| **msrp_reviewer** | `request_peer_review` | synthesis text | MSRP critique |
| **driver** | always | clean; only syntheses | final answer |

---

## Implementation Phases

### ✅ Phase 0 (DONE): Unified execution path
- `AgenticDriver.run_streaming()` as single entry point
- `_build_loop_executor()` routes all tools
- CLI, WS, HTTP all use same path

### ✅ Phase 1 (THIS COMMIT): Multi-agent conversation foundation
- `AgentTurn` + `SideData` events in `events.py`
- `_spawn_agent()` — ephemeral sub-loop factory
- `consult_bibliotecario` tool — isolated literature analysis
- `consult_specialist` tool — hub routing with streaming visibility
- `request_peer_review` tool — MSRP critic as spawned agent
- `agent_hub.route_streaming()` — streaming version of route()
- CLI display: per-agent color + icon (🔬🧠⚗️📋)

### 🔲 Phase 2: Side panel protocol
- `SideData` consumed by Alejandria WS consumer
- Scrollable paper index in UI (jump to paper → back to index)
- PDF viewer side panel with page navigation
- Structure diff viewer (PDB before/after)

### 🔲 Phase 3: Agent memory + state across turns
- Bibliotecario remembers what it already read in a session
- Driver can ask same bibliotecario multiple follow-up questions
- AgentHub sticky sessions (reuse running sub-loop instead of spawning new)

### 🔲 Phase 4: Proactive multi-agent
- `detectors.py` gap detection → automatically spawns bibliotecario
- ProactiveSystem emits `AgentTurn(agent="proactive_monitor", role="alert", ...)`
- Driver sees alert → decides whether to investigate

### 🔲 Phase 5: Agent personas + MSRP debate
- Multiple reviewers with different MSRP personas (Dr. Petrov skeptic, Dr. Sharma evidence)
- Reviewers can debate synthesis between themselves before reporting to driver
- Full peer-review loop visible in UI as dialogue

---

## Context Budget Rules

```
Driver main context:    ≤ 30k tokens active (compacts at 75%)
Per sub-agent context:  ≤ 20k tokens (corpus + task + tools)
Synthesis returned:     ≤ 800 tokens per consult call
Side data payload:      unlimited (goes to UI, never to LLM)
```

---

## CLI Display Format

```
◆ STEP 1  @ 14:32:05
  🌊  consult_bibliotecario  query=WNK1 autoinhibition  task=identify mechanism

  🔬 BIBLIOTECARIO thinking... [step 1]
  🔬 BIBLIOTECARIO → cite_finding(paper_id="Murthy2022", finding="PF1 loop...")
  🔬 BIBLIOTECARIO → identify_gap(gap_type="contradiction", desc="Zagórska vs...")
  🔬 BIBLIOTECARIO →  Los papers 2019-2024 convergen en que el dominio PF1...

  📑 SIDE PANEL [research] 87 papers → UI

     ↳ synthesis 612 tokens  4231ms

◆ STEP 2  @ 14:32:12
  🌊  consult_specialist  specialist=biodynamo  task=simulate K233E variant 4BWJ

  ⚗️  BIODYNAMO thinking... [delegado por driver]
  ⚗️  BIODYNAMO →  RMSD 2.8Å en loop activación. Confirma desestabilización.

     ↳ specialist result  2890ms
```
