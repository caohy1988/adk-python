# Talking track — governing Conversational Analytics with model-authored workflows

A short narrative for walking a technical-leadership audience through the demo.
It maps each beat to the argument it settles. (Generic framing — fill in your own
customer examples when you present.)

## The ask, and why the obvious answer fails

A recurring enterprise request: *"restrict Conversational Analytics to our
governed / golden / verified queries"* — for accuracy and for cost control. Some
customers want a hard boundary (golden-only); others want "constrained but
flexible."

The tempting answer is to **instruct the model** ("only use golden queries").
That does not hold: a prompt is a request, not a constraint. An LLM under
pressure, an injected instruction, or a confidently-wrong plan will draft fresh
SQL anyway. **Governance you can't enforce isn't governance.**

## The mechanism: governance is a registry, not a prompt

The model-authored-workflow engine gives us the enforcement point for free. A
plan is a typed `WorkflowSpec` that may only compose **capabilities registered in
a `CapabilityRegistry`**, and the `WorkflowSpecValidator` **rejects** any plan
referencing a capability that is not registered — *before anything runs*.

So "golden-only" is just a registry without a SQL-drafting capability:

```
STRICT (golden) : match_verified_query · run_frozen_query · summarize · refuse
FLEXIBLE        : … + nl2sql · dry_run · run_adhoc · reject_invalid
```

Neither registry has a promote capability — **a model-authored plan cannot write
to the governed pool.** Flipping the governance dial is swapping the registry you
hand the validator — auditable, diffable, testable. The model is never trusted to
restrain itself, and it can never enlarge its own golden set.

**One more thing — the plan is model-authored, live.** In each data beat below,
the planner is an `LlmAgent(output_schema=WorkflowSpec)`: **the model authors the
typed plan at runtime** (RFC #93's headline), and *then* the registry + validator
govern it. So this isn't a hand-wired graph being gated — it's a model-authored
dynamic workflow being governed. (The plan *shape* is instruction-guided for
on-camera reliability, with a deterministic fallback; free-authoring evidence is
in the sibling spike samples.)

## The beats

1. **`show modes registry diff`** — governance is a one-line capability
   difference, not a sprawling prompt. *(The dial.)*

2. **`adversarial: …just write SQL`** — the **model authors** a plan that drafts
   fresh SQL (🧠 model-authored, live). Under STRICT it is **rejected at
   validation** (`unknown capability 'nl2sql'`); the *same plan* validates under
   FLEXIBLE. **Proof you can't prompt your way past governance** — even the
   model's own authored plan is stopped by the validator, structurally.

3. **`What is total revenue by country? (strict)`** — a **governed hit**: the
   **model authors** the typed plan (🧠 live), it matches a verified query, and a
   **frozen, auditable workflow** runs the analyst-approved SQL on **real
   BigQuery**. Deterministic numbers, replay the same plan, `0 model-drafted SQL`.
   *(Model-authored dynamic workflow + governance, delivered.)*

4. **`…churn cohorts… (strict)`** — no verified query matches, so STRICT
   **refuses** rather than guessing. `0 queries run`. *(A hard boundary that
   fails safe.)*

5. **The middle ground + human-in-the-loop, live** — three turns:
   - `What is the average sale price by product department? (flexible)` — no
     verified query matches, so FLEXIBLE generates SQL under **semantic
     constraints**, **validates it with a real dry-run gate** (invalid SQL is
     rejected — never run), runs it, answers, and **parks it pending approval**.
     The model has *no promote capability*, so it cannot add it to the pool.
   - `approve` — a **human** signs off; the validated query **enters the governed
     pool**. (`reject` would discard it.)
   - `What is the average sale price by product department? (strict)` — the
     *same* question is now a **governed hit**. *(Assisted authoring with
     governed change control: the model proposes, a human approves, and the
     golden set grows from real usage — every answer still a frozen, auditable
     workflow, not a turn-by-turn agent run.)*

6. **`…churn cohorts… (open mode)`** — the *same* question as beat 4, dial
   turned to OPEN, falls through to a **normal agentic agent** that autonomously
   queries BigQuery and answers free-form. Powerful, but **not** a frozen,
   auditable workflow — that is the explicit trade-off the customer chooses per
   their policy. *(Both surfaces, one agent.)*

## On the FLEXIBLE middle ground (beat 5)

Between "golden-only" and "anything goes" is the constrained-yet-flexible path:
match a verified query first; on a miss, allow a **semantics/graph-constrained**
`nl2sql`, **gate** it on a real dry-run, run it — then a **human approves** before
the validated result enters the governed pool. The model never self-promotes
(there is no promote capability). The governed set **grows from real usage**,
under human change control — assisted authoring — and every answer remains a
frozen,
replayable, auditable workflow rather than an un-reconstructable turn-by-turn
agent run.

## Why this is the right enterprise story

- **Enforcement, not instruction.** The boundary is a validated property of the
  plan, provable and testable — not a hope about model behavior.
- **Auditability.** A `FrozenWorkflowRecord` is portable, hash-verified, and
  re-validated on import (drift fails loudly). Every governed answer traces to an
  approved query.
- **A dial, not a binary.** Strict golden-only, constrained-flexible, and full
  agentic are the *same agent* with a different registry — meeting customers
  wherever they sit on the control/flexibility spectrum.
- **Complementary to semantics.** Semantic models/graphs constrain *what valid
  SQL looks like*; this layer constrains *what the agent is allowed to do at
  all*. Use both.
