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
   registered capabilities (reviewer, verifier, triager, formatter).

📋 Authored plan (pipeline → step → step):
   {
     "goal": "Audit files and format the report",
     "steps": [
       {"kind": "pipeline", "id": "review_pipeline",
        "over": {"source":"task","path":"files"},
        "stages": [{"capability":"reviewer"}, {"capability":"verifier"}],
        "collect": "list"},
       {"kind": "step", "id": "triage_step",   "input": {"source":"step","step":"review_pipeline"}, "capability": "triager"},
       {"kind": "step", "id": "format_step",   "input": {"source":"step","step":"triage_step"},     "capability": "formatter"}
     ],
     "output": {"source": "step", "step": "format_step"}
   }
```

> "The model emitted a *typed plan*, not code — a **pipeline** over the files
> (`reviewer → verifier` per file, barrier-free), then `triager`, then
> `formatter`, with explicit data bindings between steps. The pipeline is the
> construct that lets each file flow review→verify independently — item A can be
> verifying while item B is still being reviewed."

## Beat 2 — validate (capability allow-list)

```
✅ Validation passed. Capabilities referenced (all registered):
   ['formatter', 'reviewer', 'triager', 'verifier'].
```

> "Validation confirms every capability the plan names is in the registry. The
> model can only compose pre-approved capabilities — no arbitrary calls, no code
> execution. That's the security model: capability allow-listing, not a sandbox."

## Beat 3 — freeze (State tab)

```
🔒 Frozen spec persisted to session state — hash 71997cdf0669.
   Re-send the prompt: it replays this exact plan, not a new one.
```

> "Open the **State** tab: `authored_workflow:frozen_spec` and `…_hash`. The plan
> is now durable data you can store, diff, and audit."

*(Presenter note: session **state** keeps a minimal `{spec, hash}` subset so the State tab stays readable. The **export** beat below serializes the full `FrozenWorkflowRecord`. Production v1 would persist the full record to state too — see `authored_workflow_spike/DESIGN.md` §5. The split here is presentational, not the canonical contract.)*

## Beat 3b — export the plan (the enterprise artifact)

```
📦 Exported plan → security_audit_plan.json (full 71997cdf0669, schema v1,
   planner gemini-3.5-flash). Re-imported OK — import recomputes the hash and
   re-validates against the current registry, never trusting the envelope's own
   validation. This is the reviewable / diffable / replayable audit artifact.
```

> "The frozen plan isn't just in-memory state — it serializes to a **portable
> JSON envelope**: the spec, its `sha256`, the planner model, registry +
> per-capability versions, the validation result, and a *digest* of the task
> input (not the raw input). `cat security_audit_plan.json` — this is the thing
> you check into a repo, diff in a PR, and hand to an auditor. And import is
> **defensive**: it recomputes the hash (rejects a tampered spec), re-validates
> against the *current* registry (rejects a dropped capability), and flags
> per-capability version drift — it never trusts the envelope's own `validation`
> stamp. That defensive import is exactly what makes a model-authored plan safe
> to store and replay later."

Show the file on camera:

```bash
cat security_audit_plan.json | jq '{schema_version, spec_hash, planner_model, capability_versions, validation}'
```

## Beat 3c — lower the static subset to ADK config

```
🧬 ADK config lowering (static subset) — 2/3 top-level steps project to ADK
   config; dynamic blocks stay SpecInterpreter-only: ['pipeline'].
   { "agent_class": "SequentialAgent", "name": "security_audit_planner",
     "sub_agents": [
       { "agent_class": "<no-AgentConfig-equivalent>", "workflowspec_kind": "pipeline", … },
       { "agent_class": "LlmAgent", "name": "triage_step",  "capability": "triager" },
       { "agent_class": "LlmAgent", "name": "format_step", "capability": "formatter" } ] }
```

> "This is the convergence with ADK config, made concrete. The static parts are
> what the `loop_config/root_agent.yaml` style is good at: a known Workflow graph
> and known child agents. This demo projects the top-level sequence onto that
> family of config shapes, with leaves referenced by **capability name, not an
> importable FQN**. The `reviewer → verifier` **pipeline** is flagged
> `<no-AgentConfig-equivalent>` because it is per-item over a runtime list; raw
> YAML would need a wrapper node, while `WorkflowSpec` keeps it typed and
> policy-checked. Honest framing: this is an *illustrative projection* (RFC #93
> §11), not a loadable `root_agent.yaml`; execution still runs through the
> interpreter."

## Beat 4 — execute (Events / trace tab)

```
📄 Audit result: Identified 4 vulnerabilities: 1 critical (command injection),
   2 high (hardcoded credentials and SQL injection), and 1 medium (division by zero).
```

> "Open **Events**: ADK runs the plan on the real engine via the #92 supervisor.
> Note the interleaving — `reviewer` and `verifier` events alternate **per
> file** (a file is being verified while another is still under review); that's
> the barrier-free pipeline, not two separate fan-out waves. Then `triager` over
> all verified findings, then `formatter`. The findings are real: a CRITICAL
> `os.system` injection, HIGH hardcoded creds and SQL injection, and a MEDIUM
> divide-by-zero."

## Beat 5 — reproduce (re-send the same prompt)

```
♻️ Reusing frozen plan from session state — hash 71997cdf0669.
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
| 1 (author)  | `false`  | `71997cdf0669` |
| 2 (re-send) | `true`   | `71997cdf0669` |

Same hash, `reused` flips to `true` — the model is not called the second time.

## Close (~20s)

> "So: a model authored a typed, validated, capability-bounded plan; ADK executed
> it on the real engine; the plan **exported** to a portable, defensively-imported
> audit artifact; and a re-send replayed the exact frozen plan. The deterministic
> test suites — 11 (#92) + 25 (#93) + 5 (demo) — lock all of this in CI, including
> the no-LLM reuse path and the export round-trip / tamper / drift checks."

**Convergence with ADK Workflow config / `root_agent.yaml`** — this is what Beat 3c shows, if a reviewer asks "why not author `loop_config/root_agent.yaml`?":

> "`loop_config/root_agent.yaml` is a good **derived target** for static graph
> structure: it has `agent_class: Workflow`, fixed `edges`, child YAML files, and
> route functions like `.agent.route_headline`. It is not the right **raw model
> output** because those refs are exactly what we don't want a model to invent:
> Python functions, `_code` refs, child config paths, tools/callbacks, or FQNs.
> #93 keeps the planner output closed and allow-listed, then lowers static parts
> toward config. The `reviewer → verifier` pipeline stays a first-class
> `WorkflowSpec` block because it dispatches per item over a runtime list; raw
> YAML would need a wrapper. The lowering shown is illustrative, not a loadable
> `root_agent.yaml`; a full config compiler is future work. `Workflow` itself is
> not deprecated, but the current config loader path and agent-config sugar
> classes are `@deprecated` + `@experimental`, so this is convergence with the
> Workflow config *shape* for compatibility, not a bet on today's loader or
> deprecated sugar."

## Proof commands (terminal, ~60s)

```bash
pytest contributing/samples/workflows/dynamic_supervisor_spike/test_dynamic_supervisor_spike.py -q  # 11
pytest contributing/samples/workflows/authored_workflow_spike/test_authoring.py -q                  # 25
pytest contributing/samples/workflows/authored_workflow_demo/test_demo_agent.py -q                  # 5
```
