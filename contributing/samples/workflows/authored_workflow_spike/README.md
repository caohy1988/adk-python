# Authored Workflow Spike — demand gate for RFC #93

Reference spike for **agent-authored typed Workflows** (RFC #93): a model emits
a declarative, validated `WorkflowSpec` (typed data, **not** code) that the
framework validates and executes on the real ADK Workflow engine via the #92
`DynamicNodeSupervisor`. This directory is the re-runnable demand-gate artifact
behind the RFC's "can a model author good plans?" question.

## Environment

- ADK: `2.1.0`
- Built against `google/adk-python` upstream `main`.
- Python 3.11+ (recursive `kind`-tagged unions; `asyncio.TaskGroup` in #92).

## Files

| File                         | Purpose                                                                                                                                                                                                                                                                          |
| ---------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `authoring.py`               | `WorkflowSpec` (plain `kind`-tagged recursive tree), `CapabilityRegistry`, `WorkflowSpecValidator`, `SpecInterpreter` (step / fan_out / pipeline / branch / loop_until), and `FrozenWorkflowRecord` / `export_plan` / `import_plan` (portable plan envelope + defensive import). |
| `test_authoring.py`          | Deterministic, CI-safe tests (no LLM). The trustworthy artifact.                                                                                                                                                                                                                 |
| `test_live_planner_sweep.py` | OPTIONAL env-gated live planner sweep across plan shapes.                                                                                                                                                                                                                        |

## Deterministic tests (CI-safe, no network)

```bash
pytest contributing/samples/workflows/authored_workflow_spike/test_authoring.py -q
```

Expected: **31 passed** — `Binding` invariant, `max_iters>=1`, validator accepts a
valid spec and rejects unknown capability / non-preceding binding / duplicate id,
the open-map warning, and interpreter execution of fan_out→aggregate, **pipeline (barrier-free per-item review→verify, plus per-stage `max_fan_out` enforcement)**, branch
(correct route), and loop_until (stops + correct output); plus **plan export/import**
(round-trip replays the same hash; import rejects a tampered spec, a dropped
capability, capability/registry version drift, an unsupported schema_version,
and a new input with no template schema); plus **ADK config lowering** of the
static subset (an illustrative projection toward static Workflow/agent config
shapes: sequence/loop/leaf by capability name; runtime fan_out/pipeline/branch
flagged no-equivalent rather than fabricated); plus **pattern coverage**
(adversarial verification and tournament via loop-carried `init`, incl. the
no-`init` validation error) and **plan-quality lints** (same-capability
self-review and unsynthesized fan-out warn; an independent plan lints clean).

## Pattern coverage — the six coordination shapes

The six empirically common coordination patterns ([Dynamic Workflows: scaling
complex work](https://aipractitioner.substack.com/p/claude-dynamic-workflows-scaling))
are all expressible in the v1 vocabulary, with deterministic tests:

| Pattern | `WorkflowSpec` expression | Test |
|---|---|---|
| classify & route | `StepRef(classifier)` → `Branch` | `test_interpreter_branch_takes_correct_route` |
| fan-out / synthesize | `FanOut` → `StepRef(synthesizer)` | `test_interpreter_fanout_then_aggregate` |
| generate & filter | `FanOut(generate)` → `StepRef(filter)` | same shape as above |
| loop until done | `LoopUntil` + `until_capability` | `test_interpreter_loop_until_stops_and_outputs` |
| adversarial verification | `FanOut(skeptics)` → threshold/filter step | `test_pattern_adversarial_verification` |
| tournament | `LoopUntil(init=…, body=[pair_maker, FanOut(judge)])` | `test_pattern_tournament_loop_carried` |

## Live planner sweep (optional evidence)

Skipped unless configured — no hardcoded project/model:

```bash
export SPIKE_LIVE=1 GOOGLE_GENAI_USE_VERTEXAI=1
export GOOGLE_CLOUD_PROJECT=<project> GOOGLE_CLOUD_LOCATION=global
export SPIKE_GEMINI_MODEL=gemini-3.5-flash   # 3.5 serves from `global`
pytest contributing/samples/workflows/authored_workflow_spike/test_live_planner_sweep.py -q -s
```

## Gate results (run on `gemini-3.5-flash`)

**Initial gate (codebase audit):** planner authored a valid, sensible, executable
plan (`fan_out reviewer → triager`) matching a hand-wired baseline. **PASS.**

**Shape sweep (this directory):** the planner authored + validated + executed all
three shapes:

| Shape       | Authored steps          | Result                                  |
| ----------- | ----------------------- | --------------------------------------- |
| multi-stage | `fan_out → step → step` | report → formatted note                 |
| branch      | `step → branch`         | took the matched route, produced a note |
| loop_until  | `loop_until`            | iterated to a headline                  |

## Findings that fell out (and shaped the RFC)

1. **Open-ended `dict[str, X]` maps are a structured-output reliability hazard.**
   Surfaced **twice**: a capability's `counts: dict[str,int]` came back empty, and
   the spec's own `Branch.routes: dict[str, list]` came back empty. **Both fixed by
   using enumerated/list structures** — capability outputs use fixed severity
   fields; `Branch.routes` is now a `list[Route]`, not a map. The validator also
   warns on open-map capability outputs.
1. **The strict `unmatched=fail` branch contract earns its keep** — when the planner
   bound a branch switch to a whole object instead of its field, execution failed
   loudly instead of silently mis-routing.
1. **Gemini `response_schema` rejects Pydantic's `Field(discriminator=...)`.** The
   plan vocabulary is a PLAIN union of models that each carry a `kind` literal (a
   *structurally-tagged* union). The strict discriminated form emits a
   `discriminator` keyword that genai's `response_schema` refuses
   (`Schema: extra_forbidden`, verified on `gemini-3.5-flash`); the `kind` tags
   still make parsing and switching unambiguous.
1. **Planning vs capability quality are separable** — authoring/structure was
   reliably good; the residual variance was per-capability output quality
   (prompts/schemas/retries), not planning.
1. **The pattern-coverage sweep surfaced a real vocabulary gap.** The tournament
   shape (pairs recomputed each round from the prior round's winners) needs
   **loop-carried state**, which the binding-scope rules statically forbade.
   Fixed with `LoopUntil.init` + the rule that a body binding to the loop's own
   id reads the carried value (validation error without `init`). This is why
   gate tasks should be selected per coordination pattern, not ad hoc — single
   tasks don't find these gaps.
1. **Typed bindings make agent independence statically checkable.** The
   validator now lints same-capability self-review (self-preferential bias)
   and unsynthesized fan-out; `independence_facts()` derives the positive
   provenance statements (e.g. *"stage `verifier` sees ONLY stage `reviewer`'s
   per-item output"*) the frozen record can prove to an auditor. Model-authored
   orchestration *code* cannot be checked this way.

This is a demand-gate artifact, not production code.
