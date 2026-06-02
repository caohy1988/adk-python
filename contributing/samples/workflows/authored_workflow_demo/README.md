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
1. **Execution** — the **Events / trace** view shows `reviewer` and `verifier` interleaving **per file** (the barrier-free pipeline), then `triager`, then `formatter`.
1. **Final output** — the triaged audit (1 CRITICAL + 2 HIGH + 1 MEDIUM across `auth.py`/`db.py`/`net.py`/`math.py`).

(Re-send the same prompt to show resume reuses the frozen spec — same hash, not re-authored.)

## 3. Shape sweep — not a one-off (1–2 min)

```bash
SPIKE_LIVE=1 pytest \
  contributing/samples/workflows/authored_workflow_spike/test_live_planner_sweep.py -q -s
```

Proof points: multi-stage `fan_out → step → step`; branch `step → branch`; loop `loop_until`.

## 4. Correctness proof (60s)

```bash
pytest contributing/samples/workflows/dynamic_supervisor_spike/test_dynamic_supervisor_spike.py -q  # 11
pytest contributing/samples/workflows/authored_workflow_spike/test_authoring.py -q                  # 14
pytest contributing/samples/workflows/authored_workflow_demo/test_demo_agent.py -q                  # 4
```

- Deterministic suites: #92 **11** + #93 **14** + demo **4** = **29** (incl. a no-LLM reuse-path test).
- PR #3 CI green except the documented fork-only `agent-triage` token job.

## Recording notes

- macOS `Cmd+Shift+5` or Loom; browser at 110–125% zoom, terminal font 16+.
- Hide project IDs / env vars. Keep it under ~7 minutes.
