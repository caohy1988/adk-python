# Technical Design — Agent-authored typed Workflows (RFC #93)

Canonical technical design for RFC #93 (GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK#93). Mirrors the issue's Technical Design comment. Covers the data model, validator, interpreter/compilation, frozen-spec contract, security model, framework changes, testing, and the empirical findings that shaped it. Audience: implementers / technical reviewers.

> **Phasing (MVP-first).** Ship **#92 first**; build full #93 only once leadership commits it as a product bet **and** 3–5 real tasks beat hand-wired workflows. **MVP scope** = `WorkflowSpec` + validator + **freeze/replay + export**; **defer** templates (v2), complex loops, and broad compiler features. (Strategic rationale: the concise RFC's *Positioning & priority*.)

## 1. Data model — `WorkflowSpec`

A plain `kind`-tagged, recursive, ordered **tree of blocks** (not a graph with jumps). `id`s are globally-unique **binding names** for dataflow, never jump targets — which removes join / fall-through / GOTO ambiguity by construction.

```python
# src/google/adk/workflow/authoring/_spec.py  (NEW)

# Typed dataflow: a Binding is the ONLY way a node sources input — a source +
# optional dotted path, validated against the producer's output schema.
class Binding(BaseModel):
  source: Literal["task", "step"]      # the workflow task input, or a prior step's output
  step: str | None = None              # REQUIRED iff source == "step"; None when source == "task"
  path: str | None = None              # optional dotted field path; checked vs the schema
  # model_validator enforces: (source == "step") == (step is not None)

class StepRef(BaseModel):
  kind: Literal["step"]
  id: str
  capability: str                      # MUST resolve in the registry
  input: Binding                       # validated against the capability's input_schema

class FanOut(BaseModel):
  kind: Literal["fan_out"]
  id: str
  over: Binding                        # MUST resolve to a LIST-typed value
  capability: str                      # run once per element (compiles to ctx.pipeline/parallel)
  collect: Literal["list"] = "list"    # per-item outputs aggregate to an order-preserving list

class PipelineStage(BaseModel):
  capability: str                      # MUST resolve in the registry; takes an item
  input: Binding | None = None         # defaults to the previous stage's per-item output

class Pipeline(BaseModel):
  kind: Literal["pipeline"]
  id: str
  over: Binding                        # MUST resolve to a LIST-typed value
  stages: list[PipelineStage]          # each item flows through ALL stages, BARRIER-FREE
  collect: Literal["list"] = "list"    # outputs aggregate to an order-preserving list
  # Compiles to #92 ctx.pipeline: item A may be in stage k while item B is in stage 1.
  # Failed item -> None; control exceptions follow #92. stage[0] input defaults to the
  # per-item element; stage[n] input defaults to stage[n-1]'s per-item output.

class Route(BaseModel):
  value: str                           # the switch value this route matches
  block: list["SpecNode"]              # non-empty; output = block's last-node output

class Branch(BaseModel):
  kind: Literal["branch"]
  id: str
  on: Binding                          # switch value; MUST resolve to a STRING/STR-ENUM schema
  routes: list[Route]                  # ENUMERATED LIST, not an open dict[str, ...] map (see Findings)
  unmatched: Literal["fail"] = "fail"  # unmatched value at runtime = FAIL (no runtime re-plan in v1)

class LoopUntil(BaseModel):
  kind: Literal["loop_until"]
  id: str
  body: list["SpecNode"]               # non-empty; loop output = LAST iteration's last-node output
  until_capability: str                # MUST declare a STRICT-bool output schema
  until_input: Binding                 # predicate input (validated vs until_capability.input_schema)
  max_iters: int = Field(ge=1)         # REQUIRED, >= 1

# PLAIN union, each member carrying a `kind` Literal (structurally-tagged) — NOT
# Annotated[..., Field(discriminator="kind")]: the discriminated form emits a
# JSON-schema `discriminator` keyword that Gemini's response_schema rejects
# (Schema: extra_forbidden — verified). `kind` still disambiguates parsing.
SpecNode = Union[StepRef, FanOut, Pipeline, Branch, LoopUntil]

class WorkflowSpec(BaseModel):
  goal: str
  steps: list[SpecNode]                # ordered blocks (sequence by list order)
  output: Binding                      # terminal output selection (validated)
```

**Block-output rule:** a block's output is its **last node's** output — so a `Branch`'s output is the taken route's last-node output, and a `LoopUntil`'s is the last iteration's last-node output. This gives every composite node a well-defined output schema, which is what makes `Binding(source="step", step=<branch/loop id>)` schema-checkable.

**Binding scope:** a `Binding` may reference only a step that lexically precedes it on the **same** root-to-node path (ancestors + earlier same-level siblings). References into a not-taken sibling route, or to a later step, are rejected at validation.

## 2. The agent

```python
# src/google/adk/workflow/authoring/_agent.py  (NEW)
class AuthoredWorkflowAgent(BaseAgent):
  planner_model: str
  registry: CapabilityRegistry          # the ONLY capabilities a plan may reference
  max_replans: int = 1

  async def _run_async_impl(self, ctx):
    frozen = await self._load_frozen_spec(ctx)           # resume: reuse the SAME spec, never re-plan
    if frozen is None:
      spec = await self._author(ctx)                     # LlmAgent(output_schema=WorkflowSpec)
      WorkflowSpecValidator(self.registry).validate(spec)
      frozen = self._freeze_and_persist(ctx, spec)       # see Frozen-spec contract
    workflow = WorkflowCompiler(self.registry).compile(frozen.spec)   # -> a real Workflow
    async for event in workflow.run_async(ctx):          # deterministic + resumable
      yield event
```

- **Authoring** = `LlmAgent(output_schema=WorkflowSpec)`; ADK validates structured output, so a malformed plan is caught and re-planned (bounded by `max_replans`).
- **Validation** is a **new semantic validator** (below) that *lowers to* `Graph.validate_graph()` for structural checks.
- **Compilation** lowers the block tree: sequence → edges; `Branch` → conditional route edges over nested blocks; `FanOut` → `ctx.parallel`-map; `Pipeline` → barrier-free `ctx.pipeline` (multi-stage); `LoopUntil` → bounded loop. The compiled artifact is an ordinary `Workflow` — nothing downstream knows it was machine-authored.
- **Registry** = developer-supplied capabilities (an agent, or a tool wrapped as a node), each with per-capability policy.

## 3. Validator — semantic, then structural

`WorkflowSpecValidator` checks what `Graph` cannot, then lowers:

- capability refs resolve in the registry;
- `Binding` invariant + path/type compatibility vs the producer's `output_schema` and consumer's `input_schema`;
- `FanOut.over` resolves to a list; the fan-out capability takes an item;
- `Branch.on` is string/str-enum-typed; route blocks share a compatible last-node output schema; non-exhaustive enum domain is flagged (unmatched at runtime fails);
- `Pipeline`: `over` resolves to a list; every stage `capability` is registered and takes an item; stage[0] input defaults to the per-item element, stage[n] to stage[n-1]'s output; the last stage's output type defines the pipeline output (validated for downstream bindings);
- `LoopUntil`: strict-bool `until_capability`, present/compatible `until_input`, `max_iters >= 1`;
- globally-unique `id`s; binding-scope (no non-preceding / cross-route references);
- registry-version match vs a frozen spec (drift = hard error).

Then **`Graph.validate_graph()`** (reused) handles duplicate names, `START`/reachability, duplicate edges, unconditional cycles on the compiled graph.

## 4. Semantics

- **Authoring non-deterministic; execution deterministic.** Once frozen, execution + resume replay is fully deterministic (it's just a `Workflow`).
- **Reuses #92 + the engine wholesale.** Fan-out → supervised `ctx.pipeline`/`ctx.parallel` (bounded, interrupt-safe); sequence/branch → edges + routes; loop → bounded loop. No new executor.
- **`Pipeline` is barrier-free per-item** (compiles directly to #92's `ctx.pipeline`): item A may be in stage *k* while item B is in stage 1; an ordinary failure drops that item to `None`; control exceptions follow #92. This closes the gap where the vocabulary was *less* expressive than its own executor — a single-capability `fan_out` is parallel-map; `Pipeline` is the multi-stage barrier-free form.
- **Re-plan is pre-execution-only.** `max_replans` applies only to validation failures; an execution failure fails the frozen run; recovery = a new explicit run/version. No recursive planner-spawning-planner.
- **Budget + agent caps from #92** bound a mis-plan's spend.

## 5. Frozen-spec contract (correctness requirement)

Persist **one** `FrozenWorkflowRecord` before any execution — the *same* shape backs session state, the audit event, and the export envelope (§10), so v1 storage is never a weaker subset:

```python
class FrozenWorkflowRecord(BaseModel):
  schema_version: str                 # "v1"
  spec: WorkflowSpec
  spec_hash: str                      # sha256(canonical_json(spec)) — see §10
  planner_model: str
  registry_version: str
  capability_versions: dict[str, str]
  validation: ValidationResult        # {passed: bool, warnings: [...]}
  created_at: str                     # ISO-8601, stamped at freeze
  task_input_schema: dict | None      # expected root task-input schema (enables template reuse)
  task_input_digest: str | None       # sha256(canonical_json(task_input))
```

Deterministic replay holds **only** if resume loads the **same** record → **resume MUST reuse it and MUST NOT re-plan** unless the user starts a new run; a registry/capability-version mismatch on resume is a hard error.

- **Storage target (v1):** the **full record** in session state under an **unprefixed (session-scoped) key** `authored_workflow:frozen_record` — not just `{spec, hash}`, so drift detection and audit have everything they need. **Not** `app:` (app-scoped — `State.APP_PREFIX`, extracted in `_session_util.extract_state_delta` — would leak per-run data and break per-run resume).
- **Audit event shape:** persist **state-only** — `Event(state={"authored_workflow:frozen_record": record})`. **Not** `Event.output` (`NodeRunner._track_event_in_context` sets `ctx.output = event.output`; `Context.output` rejects a second output → "Output already set"). **Not** `Event.content` (would re-enter a model's context).
- **Demo vs production:** the committed demo persists only a minimal `{spec, hash}` subset to keep the walkthrough readable — **it illustrates the behavior; production v1 would store the full `FrozenWorkflowRecord`.** The demo is illustrative, not the canonical contract.

## 6. Security model

Going declarative **eliminates the code-execution / sandbox-escape class** — but **not** all risk (bad args, prompt-injected inputs, side-effectful tools, expensive fan-out/loops). Controls = validation **+ per-capability policy**:

- **Capability allow-list** — non-registry refs rejected at validation.
- **No code execution** — nothing to sandbox.
- **Per-capability policy** (registry-declared): `max_calls`, `max_fan_out`, allowed caller/edge constraints, `side_effect` (requires explicit approval to appear in a plan), argument constraints/schema. **Static vs runtime split:** the validator enforces statically-knowable policy (static call counts, `max_iters`, side-effect approval, caller/edge, arg schemas); runtime enforces data-dependent caps before dispatch (`max_fan_out` vs actual list size, realized branch-path call counts).
- **Output-schema guidance (from the spike):** registered capabilities should avoid open `dict[str, X]` output maps (Gemini fills them unreliably); the registry/validator SHOULD warn, and outputs should carry invariants (e.g. counts sum to total) checked with one repair retry.
- **Per-capability permissions unchanged** — each agent runs under its own ADK tool allowlist; authoring grants no elevation.
- **Bounded blast radius** — current ADK enforces `RunConfig.max_llm_calls` (default 500); the proposed #92 limits (leaf gate, optional per-run agent cap, optional `max_tokens`) bound further; `max_iters`/`max_replans` bound loops.
- **Auditable** — frozen spec (+ hash, versions) persisted; humans can review/pre-approve.

Residual: "model composes approved capabilities, within policy, in a wasteful-but-bounded order, possibly on injected inputs" — dramatically smaller than executing model-authored Python, but **not zero**; argument-level injection into an approved side-effectful tool is the sharpest residual (hence side-effect caps default to approval-required).

## 7. Backward compatibility

Fully additive. New `authoring/` package + `AuthoredWorkflowAgent`; no change to existing agents, `Workflow`, or the engine. Opt-in; the compiled artifact is a plain `Workflow`.

## 8. Testing

- **Semantic validator rejects:** unknown capability; `Binding` invariant / incompatible path-type; `FanOut.over` non-list; `Branch.on` non-string or incompatible route output schemas; `LoopUntil` non-strict-bool predicate / missing `until_input` / `max_iters < 1`; non-preceding or cross-route binding; duplicate `id`; registry drift.
- **Structural lowering:** `Graph.validate_graph()` catches duplicate names / unreachable / unconditional cycles.
- **Frozen-spec contract:** persisted before execution; resume reuses, does not re-plan; registry-version mismatch is a hard error.
- **Per-capability policy:** plan exceeding `max_calls`/`max_fan_out` or placing an unapproved side-effect capability is rejected pre-execution.
- **Compiler:** golden test — a `WorkflowSpec` lowers to a `Workflow` matching a hand-written equivalent; fan-out → bounded `ctx.pipeline`.
- **`AuthoredWorkflowAgent`:** malformed planner output → bounded re-plan → fail past `max_replans`.
- **Determinism:** frozen spec replays identically, resumes exactly-once (inherits #92).
- **Two gates:** *planning* (valid + sensible + executable + structurally matches a hand-wired baseline) and *output-quality* (intermediate outputs match, capability invariants hold, one repair retry).

## 9. Empirical findings (from the demand-gate spike on `gemini-3.5-flash`)

1. **Gate passed.** A planner authored a valid, structurally-correct spec for a codebase audit, validated first try, executed on the real engine, matched a hand-wired baseline — across multi-stage / branch / loop_until shapes.
1. **Open-`dict[str, X]` maps are a structured-output reliability hazard** — hit twice: a capability's `counts: dict[str,int]` came back empty, and the spec's own `Branch.routes` (an open map) came back empty. **Both fixed by enumerated/list structures** (`Branch.routes` → `list[Route]`; capability outputs use fixed fields). The validator warns on open-map capability outputs.
1. **Discriminated unions are incompatible with Gemini `response_schema`** — `Field(discriminator="kind")` emits a `discriminator` keyword genai rejects (`Schema: extra_forbidden`). Use a plain `kind`-tagged union.
1. **Planner quality vs capability quality are separable** — authoring/structure was reliably good; the residual variance was per-capability output quality (prompts/schemas/retries), proven via an intermediate-output diff (authored vs baseline findings were semantically identical). The strict `unmatched=fail` branch contract also caught a bad field-binding loudly instead of mis-routing.

Re-runnable: `contributing/samples/workflows/authored_workflow_spike/` (21 deterministic tests + env-gated live sweep) and `authored_workflow_demo/` (ADK Web `root_agent` + 4 CI-safe tests incl. the no-LLM reuse path), in `caohy1988/adk-python` PR #3.

## 10. Plan export & storage — the frozen spec as a durable artifact

> **Spike status:** `export_plan` / `import_plan` / `FrozenWorkflowRecord` are **implemented** in `authoring.py` and exercised by deterministic tests (round-trip, tamper, dropped-capability, version-drift, replay-vs-template input) and a live demo "Export plan" beat. The *tiering* below remains the production roadmap.

**Source of truth = the typed `WorkflowSpec`.** The compiled `Workflow` is a *derived* artifact. Storage is tiered, scoped to keep generated code and compiled graphs out of v1:

- **v1 (required) — persist the full `FrozenWorkflowRecord` per run** (§5) under `authored_workflow:frozen_record` — for resume/replay **and** drift detection.

- **v1.1 (recommended) — export the record as a portable JSON envelope.** The envelope **is a serialized `FrozenWorkflowRecord`** (§5) — same fields, never a weaker shape — produced by an explicit "Export plan" operation:

  ```json
  {
    "schema_version": "v1",
    "spec": { "...": "the WorkflowSpec" },
    "spec_hash": "...",
    "planner_model": "...",
    "registry_version": "...",
    "capability_versions": { "reviewer": "...", "triager": "..." },
    "validation": { "passed": true, "warnings": [] },
    "created_at": "<ISO-8601, recorded at export>",
    "task_input_schema": { "...": "expected task-input JSON schema, or null" },
    "task_input_digest": "<digest of the task input, NOT the raw input>"
  }
  ```

  This is the enterprise story: a model-authored plan becomes **reviewable, diffable, auditable, replayable** data. `created_at` is stamped at export (not at replay); `task_input_digest` is a digest so a portable plan doesn't carry raw task content.

  **Digest/hash definition.** `spec_hash` and `task_input_digest` are `sha256` over **canonical JSON** — `json.dumps(value, sort_keys=True, separators=(",", ":"))` — of the spec and the task input respectively. A single fixed definition so two exporters produce identical hashes for the same logical value (no whitespace/key-order drift).

  **Execution-input contract on import.** `task_input_digest` is *advisory provenance* for replaying the **original** run. Reusing a plan against a **new** task input is template behavior: ADK validates the new input against the captured `task_input_schema`. If `task_input_schema` is null (none captured), import may only **replay** with a matching `task_input_digest`, or must go through explicit **template promotion** (which attaches a `task_input_schema`) first. A stored plan must never silently bind (e.g. `task.files`) against an incompatible task shape.

  ```python
  def export_plan(record: FrozenWorkflowRecord) -> dict: ...   # serialize the §5 record
  def import_plan(envelope, registry, *, task_input=None) -> WorkflowSpec:
    # INTEGRITY (never trust the envelope's own `validation`):
    #   1. recompute sha256(canonical_json(spec)); REJECT if != envelope["spec_hash"]
    #   2. re-run WorkflowSpecValidator against the CURRENT registry
    #   3. registry/capability drift -> fail loudly (or explicit migration)
    # EXECUTION-INPUT:
    #   replay   : task_input digest must match envelope["task_input_digest"] (else audit-only)
    #   template : task_input validated against envelope["task_input_schema"] before execution
    #   neither  : do NOT execute against arbitrary new input
  ```

- **v2 (optional) — promote an exported plan to a reusable template.** A human approves a spec and saves it as a template. **On import, ADK MUST re-validate against the *current* registry**; registry/capability drift **fails loudly or requires explicit migration** — never a silent run against a changed capability set. (The envelope's `registry_version` / `capability_versions` are what make drift detectable.)

- **Deferred — compiled `Workflow`/graph (or generated Python) as the source of truth.** The compiled `Workflow` is regenerated from the spec on demand; it is **not** stored as canonical, because compiler behavior and ADK internals evolve. Persisting generated code or a compiled graph is explicitly out of scope.

Net: this turns the proposal from "a model can author plans" into "**model-authored plans become durable enterprise artifacts**" — without committing to durable generated code.

## 11. Convergence with ADK `AgentConfig` (+ storage, custom tools, observability)

A reviewer asked whether the planner should author ADK's existing **`AgentConfig`** (the `root_agent.yaml` format) directly. Verified against source — `agents/agent_config.py`, `agents/base_agent_config.py`, `agents/{llm,sequential,parallel,loop}_agent_config.py`, `agents/common_configs.py`, `tools/_tool_configs.py` (names), and `agents/config_agent_utils.py`:

**Lower to config where it fits.** ADK config already models the *static* shapes, so the static subset **should lower to** them rather than reinvent a serialization. **This is a design direction — the spike does not yet implement an `AgentConfig`-lowering compiler.**

| `WorkflowSpec` block                      | ADK config relationship                                                |
| ----------------------------------------- | ---------------------------------------------------------------------- |
| sequence                                  | lowers to `SequentialAgentConfig` (`sub_agents: list[AgentRefConfig]`) |
| static parallel block                     | lowers to `ParallelAgentConfig` (static sub-agent list)                |
| bounded loop                              | lowers to `LoopAgentConfig` (`max_iterations`)                         |
| runtime `fan_out` / `pipeline` / `branch` | no direct config equivalent                                            |

`ParallelAgentConfig` models a **static** set of parallel sub-agents, **not** data-mapping over a runtime list — so per-item `fan_out` sits in the "no equivalent" row, not the parallel row.

**Why the planner should not emit raw `AgentConfig`:**

1. **Static graph, build-time only.** `config_agent_utils.from_config(path)` parses the YAML and resolves the *entire* `sub_agents` tree at load (`base_agent.__create_kwargs` → `resolve_agent_reference` per child), before any request. There is no runtime list iteration — so per-item fan-out, pipeline, and conditional branch routing cannot be expressed.
1. **Not a clean `response_schema`.** `AgentConfig` is a `RootModel` over a `Discriminator(agent_config_discriminator)` union; Gemini's `response_schema` rejects the emitted `discriminator` keyword (`Schema: extra_forbidden` — the spike's §9 lesson). It also carries open `extra='allow'` maps (`ToolArgsConfig`, `BaseAgentConfig.model_extra`).
1. **Trust-boundary mismatch on tool/agent refs.** Tools/agents/callbacks are named by **fully-qualified importable path** — `CodeConfig.name`, `AgentRefConfig.code`, `LlmAgentConfig.tools[].name`, `*_callbacks` — resolved via `importlib.import_module` + `getattr` (`config_agent_utils.resolve_code_reference`). That FQN-import model is appropriate for **developer-authored** config; the concern is specifically letting a **model** author raw FQNs. For model-authored plans we want **capability allow-listing**, not arbitrary import paths — a trust-boundary difference, not a flaw in config.

**Direction:** keep `WorkflowSpec` as the thin **authoring** schema (closed, allow-listed, `response_schema`-safe); lower its static subset to `AgentConfig` so those shapes share ADK's serialization and tooling; keep `branch` / `fan_out` / `pipeline` + capability allow-listing as new surface only for the dynamic and trust-boundary pieces config doesn't cover. The compiled artifact is still an ordinary `Workflow` (§2).

**Q1 — spec storage.** §5/§10: one `FrozenWorkflowRecord` in session State (`authored_workflow:frozen_record`, unprefixed/session-scoped; resume reuses, never re-plans), a state-only audit event, and a v1.1 export envelope. Compiled `Workflow` is derived, never canonical.

**Q2 — custom tools.** A custom tool is a **registered capability** referenced by **registry name** (the registry is the allow-list), carrying per-capability policy (`max_calls`, `max_fan_out`, `side_effect`→approval, arg constraints) — §6. Deliberately *not* config's FQN `tools:` field: the model never names an import path.

**Q3 — version control & observability.** Drift surface = `spec_hash` (sha256/canonical-JSON) + `planner_model` + `registry_version` + per-capability `capability_versions` in the record (§5); import hard-errors on schema-version, hash, registry-version, or capability-version drift (spike-enforced, §10). The export envelope is diffable for PR/audit review. Runtime observability is unchanged: the compiled `Workflow` runs on the real engine, so existing ADK tracing/events apply; the frozen record + hash anchor each run to its plan.

## 12. Future (post-gate, NOT MVP)

**Hierarchical / sub-plan authoring** — a registered capability that is itself an `AuthoredWorkflowAgent`, so a step can expand into its own authored sub-plan. This is the likely path to parity with Claude Code's unbounded orchestration (it lifts the single-response plan-size ceiling), but it is **out of MVP scope** and should be evaluated **only after the 3–5-task build gate**. MVP stays single-level: `WorkflowSpec` + validator + freeze/replay + export.

**Upstream `AgentConfig` extension (optional).** If the dynamic constructs prove their value, the cleaner long-term home for `branch` / dynamic `fan_out` may be **new agent-config types upstream** (a conditional agent; a runtime-fan-out agent) plus an allow-listed capability-reference tool field — at which point authoring could converge fully onto an extended `AgentConfig`. Out of scope here; depends on upstream accepting new agent classes.

## References

- #92 — supervised concurrent dynamic dispatch + `ctx.pipeline` (executor).
- Claude Code Dynamic Workflows — https://code.claude.com/docs/en/workflows
- ADK: `Workflow`/`Graph` (`src/google/adk/workflow/_graph.py`), `LlmAgent.output_schema` / `validate_schema`, `BaseAgent.run_async`, `_session_util.extract_state_delta`, `NodeRunner._track_event_in_context`.
