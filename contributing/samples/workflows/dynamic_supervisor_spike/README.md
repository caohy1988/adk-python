# Dynamic Supervisor Spike — concurrent dynamic dispatch for ADK Workflows

Reproducible harness for an RFC proposing leaf-gated concurrent dynamic
dispatch (`ctx.pipeline` / `ctx.parallel`) on the ADK Workflow engine.

**The harness exists to prove the design on the real engine, not to ship an
API.** It pins exactly which properties hold: all five merge-gate properties
hold with a wrapper supervisor on the unmodified engine. The v1 interrupt
behavior is decided — cancel in-flight siblings and re-run them on resume;
checkpoint-then-pause is a deferred v2 product decision.

## Environment

- Built/run against `google/adk-python` (branch rebased onto current `main`).
- *Historical run evidence below was captured on ADK `2.0.0` at `origin/main` @ `4006fe40`; results re-verified on the rebased branch.*
- Python: 3.11+ (uses `asyncio.TaskGroup` + `except*`)

## Files

| File                               | Purpose                                                                                                |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `supervisor.py`                    | Prototype `DynamicNodeSupervisor` (gate-on-leaf + `TaskGroup` fan-out) over the real `ctx.run_node()`. |
| `test_dynamic_supervisor_spike.py` | Deterministic regression harness (no LLM). The trustworthy artifact.                                   |
| `test_live_gemini_e2e.py`          | OPTIONAL live-model evidence; env-gated, skipped by default.                                           |

## Run the deterministic harness (CI-safe, no network)

```bash
pytest contributing/samples/workflows/dynamic_supervisor_spike/test_dynamic_supervisor_spike.py -q
```

### Expected current result: **11 passed**

1. `test_concurrent_dispatch_correct_and_barrier_free` — concurrent `ctx.run_node`
   executes correctly (distinct results, no corruption), wall ≈ max-delay not serial sum.
1. `test_pipeline_barrier_free` — item 0 enters stage 2 before item 1 finishes stage 1.
1. `test_parallel_failed_item_isolation` — ordinary error → `None`, siblings unaffected.
1. `test_control_exception_propagates_and_cancels_siblings` — `NodeInterruptedError`
   propagates **and cancels the running sibling**. Requires `asyncio.TaskGroup`:
   `asyncio.gather` propagates but does **not** cancel siblings, so the supervisor
   contract mandates TaskGroup-equivalent structured concurrency.
1. `test_nested_combinator_no_deadlock_leaf_gating` — a pipeline stage calling
   `parallel` with `gate=2` completes; peak in-flight ≤ gate.
1. `test_driver_gating_deadlocks_as_predicted` — CONTRAST: gating *drivers* instead
   of *leaves* deadlocks (timeout). Proves the leaf-gating decision empirically.
1. `test_sequential_resume_is_exactly_once` — sequential dispatch resumes
   exactly-once (completed children fast-forward; interrupted node re-runs).
1. `test_concurrent_resume_completed_children_fast_forward` — **the merge gate.**
   Under *concurrent* dispatch, children that COMPLETE before the interrupt
   fast-forward on resume (exactly-once). No double-spend.
1. `test_concurrent_inflight_children_cancelled_on_interrupt_rerun` — pins the
   **decided v1 semantic**: a sibling that interrupts while others are still IN
   FLIGHT cancels them; cancelled (never-completed) children correctly re-run on
   resume. (Checkpoint-then-pause is deferred to v2.)
1. `test_child_cancellederror_does_not_cancel_siblings` — a branch-originated
   `asyncio.CancelledError` is asyncio task-cancellation: not propagated, siblings
   untouched, slot left `None`. Only `NodeInterruptedError` / non-cancellation
   `BaseException` cancel siblings.
1. `test_gate_must_be_positive` — `gate=0`/negative raises `ValueError` at
   construction (would otherwise deadlock every dispatch).

## Resume exactly-once: there is no engine gap (a correction)

An earlier draft of this harness reported a resume "engine gap." That was a
**test artifact and has been retracted.** The earlier test let the
`RequestInput` child interrupt *before* its siblings finished, so the
`TaskGroup` cancelled still-running siblings; those **cancelled (never
completed)** children then re-ran on resume — which is *correct*, not a bug.

With the timing separated (test 8 vs test 9), the truth is:

- **Completed** concurrent children **fast-forward** on resume (exactly-once) —
  identical to sequential. No double-spend of completed LLM work.
- **In-flight** children cancelled by an interrupting sibling **re-run** on
  resume — correctness-preserving (they never completed).

The `"Workflow ...: cancelling N leftover tasks"` log is **benign cleanup** — it
appears even in the sequential exactly-once run, and completion is still
checkpointed correctly. It is not corruption.

**Net: all five merge-gate properties hold with a wrapper supervisor + the real
engine; no `_workflow.py` change is required for resume correctness.** The one
behavior worth calling out in the RFC is a design trade-off, not a bug:
interrupting one branch cancels in-flight siblings and discards their partial
progress. If preserving that progress is desired, that is a separate design
decision (e.g. checkpoint-then-pause instead of cancel).

## Optional: live model evidence (supporting only)

Skipped unless explicitly configured — never runs in CI by accident:

```bash
export SPIKE_LIVE=1
export GOOGLE_GENAI_USE_VERTEXAI=1
export GOOGLE_CLOUD_PROJECT=<your-project>
export GOOGLE_CLOUD_LOCATION=global          # gemini-3.5-flash serves here
export SPIKE_GEMINI_MODEL=gemini-3.5-flash   # or any flash model you can access
pytest contributing/samples/workflows/dynamic_supervisor_spike/test_live_gemini_e2e.py -q -s
```

Asserts the concurrent pipeline wall-clock is well under the serial sum of
per-call latencies. The deterministic engine tests — not this — are the
artifact maintainers should trust.
