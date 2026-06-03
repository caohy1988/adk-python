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
1. **Frozen spec + hash** — open the **State** tab: `authored_workflow:frozen_spec` and `…_hash`.
1. **Exported plan** — `📦 Exported plan → security_audit_plan.json`. The full `FrozenWorkflowRecord` (spec, `sha256`, planner model, registry + capability versions, validation, task-input digest) as a portable envelope; import recomputes the hash and re-validates against the current registry. `cat security_audit_plan.json | jq .` on camera.
1. **AgentConfig lowering** — `🧬 AgentConfig lowering (static subset) — 2/3 …`. The plan's static skeleton projects onto ADK `AgentConfig` shapes (`SequentialAgent` + `LlmAgent` leaves by capability name); the `reviewer → verifier` pipeline is flagged **no-AgentConfig-equivalent**, not fabricated. An illustrative projection (RFC #93 §11) — see the talking point below.
1. **Execution** — the **Events / trace** view shows `reviewer` and `verifier` interleaving **per file** (the barrier-free pipeline), then `triager`, then `formatter`.
1. **Final output** — the triaged audit (1 CRITICAL + 2 HIGH + 1 MEDIUM across `auth.py`/`db.py`/`net.py`/`math.py`).

(Re-send the same prompt to show resume reuses the frozen spec — same hash, not re-authored.)

### Relationship to ADK `AgentConfig` (talking point)

The RFC's direction is to **converge with ADK config** (RFC #93 → "Relationship to ADK `AgentConfig`"; DESIGN §11): the *static* shapes of an authored plan should lower to `Sequential`/`Parallel`/`LoopAgentConfig`, while the dynamic constructs stay `WorkflowSpec`-only. This demo's plan makes the split concrete:

- the **top-level sequence** (`pipeline → triager → formatter`) is the kind of static composition that maps to a `SequentialAgent`;
- the **`reviewer → verifier` pipeline** (per-item, barrier-free over a runtime list) is exactly what `AgentConfig` **can't** express — no `ConditionalAgent`, and `sub_agents` are resolved once at load — which is why `WorkflowSpec` exists.

The demo now **shows** this split: the 🧬 lowering beat prints the static skeleton projected onto `AgentConfig` shapes (2/3 of the demo plan), with the pipeline marked no-equivalent.

Honest scope: it's an **illustrative structural projection** (leaves by capability name, dynamic blocks flagged) — **not** a loadable `root_agent.yaml`. Execution still runs via the `SpecInterpreter` on the real engine; a full loadable-config compiler (child YAML / an allow-listed capability-ref field) is future work (DESIGN §12).

> **If asked "why build on deprecated config?"** — `AgentConfig` and the concrete config classes are currently `@deprecated` + `@experimental` in ADK source, so this is convergence with the existing config **shape** for compatibility/illustration, **not** a long-term dependency on YAML config (RFC §11).

## 3. Shape sweep — not a one-off (1–2 min)

```bash
SPIKE_LIVE=1 pytest \
  contributing/samples/workflows/authored_workflow_spike/test_live_planner_sweep.py -q -s
```

Proof points: multi-stage `fan_out → step → step`; branch `step → branch`; loop `loop_until`.

## 4. Correctness proof (60s)

```bash
pytest contributing/samples/workflows/dynamic_supervisor_spike/test_dynamic_supervisor_spike.py -q  # 11
pytest contributing/samples/workflows/authored_workflow_spike/test_authoring.py -q                  # 25
pytest contributing/samples/workflows/authored_workflow_demo/test_demo_agent.py -q                  # 5
```

- Deterministic suites: #92 **11** + #93 **25** + demo **5** = **41** (incl. a no-LLM reuse-path test).
- PR #3 CI green except the documented fork-only `agent-triage` token job.

## Recording notes

- macOS `Cmd+Shift+5` or Loom; browser at 110–125% zoom, terminal font 16+.
- Hide project IDs / env vars. Keep it under ~7 minutes.
