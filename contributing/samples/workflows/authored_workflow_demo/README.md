# ADK Web demo — model-authored typed Workflows (RFC #93)

A ~7-minute demo: a model authors a typed `WorkflowSpec`, ADK validates it
against a capability registry, freezes it to session state, and executes it on
the real ADK engine via the #92 supervisor — all visible in ADK Web's chat,
state, and event surfaces. **ADK Web sells the product fit; pytest/CI sells the
correctness.**

## 0. Configure a model (no hardcoded project)

```bash
export GOOGLE_GENAI_USE_VERTEXAI=1
export GOOGLE_CLOUD_PROJECT=<your-project>
export GOOGLE_CLOUD_LOCATION=global          # gemini-3.5-flash serves from `global`
export SPIKE_GEMINI_MODEL=gemini-3.5-flash   # or any flash model you can access
```

## 1. Thesis (20s)

- **#92** is the supervised concurrent executor (`DynamicNodeSupervisor` + `ctx.pipeline`).
- **#93** is the model-authored typed `WorkflowSpec` layer.
- The demo: a model authors a *validated* plan, then ADK executes that *frozen* plan reproducibly.

## 2. ADK Web walkthrough (3–5 min)

```bash
adk web contributing/samples/workflows/authored_workflow_demo \
  --port 8000 --session_service_uri "sqlite:///demo_sessions.db"
```

Open the UI, pick `security_audit_planner`, and send:

```text
Plan and run a codebase security review.
```

Point at the ADK-native evidence as it streams:

1. **Authored `WorkflowSpec`** — the chat shows the JSON plan (`pipeline → step → step`: a `reviewer → verifier` pipeline over the files, then `triager`, then `formatter`).
1. **Validation** — "Validation passed" + the capability list (all registered).
1. **Independence lints** — `🧪 Plan-quality lints: 0 warnings.` The typed bindings make agent isolation **statically checkable**: the verifier stage provably sees only the reviewer's per-item output (independent verification, per file), and each downstream step provably consumes only its upstream's typed output. The frozen record can *prove* these structural bias controls to an auditor — model-written orchestration code can't be checked this way.
1. **Frozen spec + hash** — open the **State** tab: `authored_workflow:frozen_spec` and `…_hash`.
1. **Exported plan** — `📦 Exported plan → security_audit_plan.json`. The full `FrozenWorkflowRecord` (spec, `sha256`, planner model, registry + capability versions, validation, task-input digest) as a portable envelope; import recomputes the hash and re-validates against the current registry. `cat security_audit_plan.json | jq .` on camera.
1. **ADK config lowering** — `🧬 ADK config lowering (static subset) — 2/3 …`. The plan's static skeleton projects toward ADK Workflow/agent config shapes (a static `Workflow`/`SequentialAgent` skeleton + `LlmAgent` leaves by capability name); the `reviewer → verifier` pipeline is flagged **no-AgentConfig-equivalent**, not fabricated. An illustrative projection (RFC #93 §11) — see the talking point below.
1. **Execution** — the **Events / trace** view shows `reviewer` and `verifier` interleaving **per file** (the barrier-free pipeline), then `triager`, then `formatter`.
1. **Final output + cost** — the triaged audit (1 CRITICAL + 2 HIGH + 1 MEDIUM across `auth.py`/`db.py`/`net.py`/`math.py`), then `📊 Cost: 10 capability dispatches in N.Ns + 1 planner call` — the planner is invoked at most once (zero on replay); all per-step work runs outside its context.

(Re-send the same prompt to show resume reuses the frozen spec — same hash, not re-authored.)

Then run the **free-authoring beat** — in a **new session**, send:

```text
Freely plan a security review of the files — decompose it yourself.
```

The planner receives ONLY the goal + capability descriptions (no plan recipe — `test_free_planner_instruction_is_recipe_free` pins this). The shape may differ run to run; that's the point — and the freeze beat then makes *this* run replayable. Talking point: *the default walkthrough shows the mechanics on a scripted plan; this beat is the honest "model-authored" claim.*

Then run the **quality-gate beat** — send:

```text
Plan a sloppy review: have the reviewer double-check its own findings.
```

The planner authors a *valid* plan (registered capabilities, typed bindings) whose pipeline is `reviewer → reviewer` — and the **plan-quality lint fires on camera**: `🚨 plan-quality: pipeline 'rev' stage 'reviewer' re-checks its own capability's output — same-capability review cannot provide independent verification (self-preferential bias)`, followed by `🛑 Plan rejected by the quality gate — NOT frozen, NOT executed`. Talking point: *plain validation passes; only the structural bias check catches it — before anything runs, and provably.*

### Relationship to ADK Workflow config / `root_agent.yaml` (talking point)

The RFC's direction is to **converge with ADK config where it fits** (RFC #93 → "Relationship to ADK Workflow config / `root_agent.yaml`"; DESIGN §11). The linked `loop_config/root_agent.yaml` sample is the right mental model for the **static** portion: a human-authored `agent_class: Workflow` YAML graph with known `edges`, child YAML files, and function refs like `.agent.route_headline`. #93 should be able to lower/export static graph skeletons toward that style, while the model-facing format stays `WorkflowSpec`.

- the **top-level sequence** (`pipeline → triager → formatter`) is the kind of static composition that can lower to a static Workflow/config skeleton;
- the **`reviewer → verifier` pipeline** (per-item, barrier-free over a runtime list) is exactly what raw YAML **doesn't express directly** today; it would need a wrapper node, while `WorkflowSpec` can keep it typed and policy-checked as a first-class runtime block;
- raw YAML can name function refs, `_code` refs, child YAML files, tools, callbacks, or importable FQNs; model-authored plans should reference only allow-listed capability names.

The demo now **shows** this split: the 🧬 lowering beat prints the static skeleton projected onto ADK config shapes (2/3 of the demo plan), with the pipeline marked no-equivalent.

Honest scope: it's an **illustrative structural projection** (leaves by capability name, dynamic blocks flagged) — **not** a loadable `root_agent.yaml`. Execution still runs via the `SpecInterpreter` on the real engine; a full loadable-config compiler (Workflow YAML edges + child YAML + an allow-listed capability-ref field) is future work (DESIGN §12).

> **If asked "why not just author `loop_config/root_agent.yaml`?"** — use that YAML shape as a lowering/export target for static graphs, not as the raw model output. The sample intentionally resolves Python function refs and child YAML refs; #93 needs a closed, response-schema-safe, capability-allow-listed authoring format first. Also, `Workflow` itself is not deprecated, but the current config loader path and agent-config sugar classes are `@deprecated` + `@experimental`; this is convergence with the Workflow config **shape** for compatibility/illustration, not a long-term dependency on today's loader or deprecated sugar (RFC §11).

## 3. Shape sweep — not a one-off (1–2 min)

```bash
SPIKE_LIVE=1 pytest \
  contributing/samples/workflows/authored_workflow_spike/test_live_planner_sweep.py -q -s
```

Proof points: multi-stage `fan_out → step → step`; branch `step → branch`; loop `loop_until`.

## 4. Correctness proof (60s)

```bash
pytest contributing/samples/workflows/dynamic_supervisor_spike/test_dynamic_supervisor_spike.py -q  # 11
pytest contributing/samples/workflows/authored_workflow_spike/test_authoring.py -q                  # 36
pytest contributing/samples/workflows/authored_workflow_demo/test_demo_agent.py -q                  # 8
```

- Deterministic suites: #92 **11** + #93 **36** + demo **8** = **55** (incl. a no-LLM reuse-path test, the six-pattern coverage sweep — adversarial verification + tournament via loop-carried `init` — the plan-quality lints with `allow_self_chain` policy + recorded waivers, contract-hash drift rejection (fail-closed on stripped hashes), and the recipe-free free-authoring instruction pin).
- PR #3 CI green except the documented fork-only `agent-triage` token job.

## Recording notes

- macOS `Cmd+Shift+5` or Loom; browser at 110–125% zoom, terminal font 16+.
- Hide project IDs / env vars. Keep it under ~7 minutes.
