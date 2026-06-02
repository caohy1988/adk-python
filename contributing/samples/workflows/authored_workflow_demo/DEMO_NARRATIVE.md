# Demo narrative — model-authored typed Workflows (RFC #93)

A beat-by-beat narration for the ~7-minute recording, with a **real transcript**
captured on Vertex `gemini-3.5-flash`. Pair this with the run commands in
`README.md`. Bottom line: *ADK Web sells the product fit; pytest/CI sells the
correctness.*

## Thesis (say this first, ~20s)

> "#92 gives ADK a supervised concurrent executor. #93 lets a model **author** a
> typed `WorkflowSpec` — a plan as *data, not code* — that ADK validates against
> a capability allow-list, freezes, and executes reproducibly. Watch the model
> write a plan, ADK validate it, run it, and then **replay the exact same frozen
> plan** without re-invoking the model."

## Beat 1 — author (ADK Web chat)

Send: **"Plan and run a codebase security review."** The chat streams:

```
🧭 Model-authored Workflow — planning a security audit over 4 files using only
   registered capabilities (reviewer, triager, formatter).

📋 Authored plan (fan_out → step → step):
   {
     "goal": "Audit files and format the report",
     "steps": [
       {"kind": "fan_out", "id": "review_files",   "over": {"source":"task","path":"files"}, "capability": "reviewer"},
       {"kind": "step",     "id": "triage_findings", "input": {"source":"step","step":"review_files"}, "capability": "triager"},
       {"kind": "step",     "id": "format_report",   "input": {"source":"step","step":"triage_findings"}, "capability": "formatter"}
     ],
     "output": {"source": "step", "step": "format_report"}
   }
```

> "The model emitted a *typed plan*, not code — a fan-out of `reviewer` over the
> files, then `triager`, then `formatter`, with explicit data bindings between
> steps."

## Beat 2 — validate (capability allow-list)

```
✅ Validation passed. Capabilities referenced (all registered):
   ['formatter', 'reviewer', 'triager'].
```

> "Validation confirms every capability the plan names is in the registry. The
> model can only compose pre-approved capabilities — no arbitrary calls, no code
> execution. That's the security model: capability allow-listing, not a sandbox."

## Beat 3 — freeze (State tab)

```
🔒 Frozen spec persisted to session state — hash 206fb4d3a27b.
   Re-send the prompt: it replays this exact plan, not a new one.
```

> "Open the **State** tab: `authored_workflow:frozen_spec` and `…_hash`. The plan
> is now durable data you can store, diff, and audit."

## Beat 4 — execute (Events / trace tab)

```
📄 Audit result: Identified 4 vulnerabilities: 1 critical (command injection),
   2 high (hardcoded credentials and SQL injection), and 1 medium (division by zero).
```

> "Open **Events**: ADK runs the plan on the real engine via the #92 supervisor —
> the `reviewer` fan-out over the 4 files, then `triager`, then `formatter`. The
> findings are real: a CRITICAL `os.system` injection, HIGH hardcoded creds and
> SQL injection, and a MEDIUM divide-by-zero."

## Beat 5 — reproduce (re-send the same prompt)

```
♻️ Reusing frozen plan from session state — hash 206fb4d3a27b.
   The model is NOT re-invoked; the exact prior plan is replayed.
✅ Validation passed. ...
📄 Audit result: ...
```

> "Send the same prompt again — **same hash, model not re-invoked**. The frozen
> plan is replayed. That's the reproducibility guarantee: authoring is a
> one-time, auditable step; execution is deterministic replay."

**Verified outputs (this capture):**

| Run         | `reused` | `hash`         |
| ----------- | -------- | -------------- |
| 1 (author)  | `false`  | `206fb4d3a27b` |
| 2 (re-send) | `true`   | `206fb4d3a27b` |

Same hash, `reused` flips to `true` — the model is not called the second time.

## Close (~20s)

> "So: a model authored a typed, validated, capability-bounded plan; ADK executed
> it on the real engine; and a re-send replayed the exact frozen plan. The
> deterministic test suites — 11 (#92) + 10 (#93) + 4 (demo) — lock all of this
> in CI, including a no-LLM test of this reuse path."

## Proof commands (terminal, ~60s)

```bash
pytest contributing/samples/workflows/dynamic_supervisor_spike/test_dynamic_supervisor_spike.py -q  # 11
pytest contributing/samples/workflows/authored_workflow_spike/test_authoring.py -q                  # 10
pytest contributing/samples/workflows/authored_workflow_demo/test_demo_agent.py -q                  # 4
```
