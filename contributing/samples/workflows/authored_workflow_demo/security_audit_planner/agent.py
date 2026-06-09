# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""ADK Web demo agent for RFC #93 — model-authored typed Workflows.

`root_agent` is a `Workflow` whose single node:
  1. asks a planner `LlmAgent(output_schema=WorkflowSpec)` to author a plan,
  2. validates it (`WorkflowSpecValidator`) against a capability registry,
  3. persists the frozen spec + hash to session state,
  4. executes it on the real ADK engine via the #92 supervisor,
surfacing each step as a chat message so the ADK Web UI shows the authored
plan, validation, capabilities, frozen hash, and final output. Run with:

    adk web contributing/samples/workflows/authored_workflow_demo

Configure a model first (no hardcoded project):
    export GOOGLE_GENAI_USE_VERTEXAI=1 GOOGLE_CLOUD_PROJECT=<project>
    export GOOGLE_CLOUD_LOCATION=global SPIKE_GEMINI_MODEL=gemini-3.5-flash
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import time
from typing import Literal

from google.adk import Agent
from google.adk import Context
from google.adk import Event
from google.adk import Workflow
from google.adk.workflow import node
from google.genai import types
from pydantic import BaseModel

# Reuse the committed #93 authoring stack (sibling sample dir).
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        "..",
        "authored_workflow_spike",
    ),
)
from authoring import agent_config_coverage  # noqa: E402
from authoring import Capability  # noqa: E402
from authoring import CapabilityRegistry  # noqa: E402
from authoring import export_plan  # noqa: E402
from authoring import FrozenWorkflowRecord  # noqa: E402
from authoring import import_plan  # noqa: E402
from authoring import independence_facts  # noqa: E402
from authoring import lower_to_agent_config  # noqa: E402
from authoring import sha256_hex  # noqa: E402
from authoring import SpecInterpreter  # noqa: E402
from authoring import WorkflowSpec  # noqa: E402
from authoring import WorkflowSpecValidator  # noqa: E402

MODEL = os.environ.get("SPIKE_GEMINI_MODEL", "gemini-2.5-flash")
DET = types.GenerateContentConfig(temperature=0)

# A small, deliberately-mixed codebase to audit (3 vulnerable, 1 safe).
FILES = [
    {
        "path": "auth.py",
        "code": "def login(pw): return pw == 'admin123'  # hardcoded",
    },
    {
        "path": "db.py",
        "code": "q = 'SELECT * FROM users WHERE id=' + request.args['id']",
    },
    {"path": "net.py", "code": "os.system('ping ' + user_supplied_host)"},
    {"path": "math.py", "code": "def mean(xs):\n    return sum(xs) / len(xs)"},
]


class Finding(BaseModel):
  path: str
  severity: Literal["CRITICAL", "HIGH", "MEDIUM", "LOW", "NONE"]
  issue: str


class ReportFixed(BaseModel):
  total: int
  critical: int
  high: int
  medium: int
  low: int
  none: int
  summary: str


class Note(BaseModel):
  note: str


def _registry() -> CapabilityRegistry:
  return CapabilityRegistry([
      Capability(
          name="reviewer",
          input_kind="item",
          output_model=Finding,
          serialize_input=True,
          max_fan_out=50,
          build=lambda: Agent(
              name="reviewer",
              model=MODEL,
              output_schema=Finding,
              generate_content_config=DET,
              instruction=(
                  "Input JSON with keys path and code. Output a Finding"
                  " (echo the path)."
              ),
          ),
      ),
      Capability(
          name="verifier",
          input_kind="item",
          output_model=Finding,
          serialize_input=True,
          max_fan_out=50,
          build=lambda: Agent(
              name="verifier",
              model=MODEL,
              output_schema=Finding,
              generate_content_config=DET,
              instruction=(
                  "Input: a Finding JSON (path, severity, issue). Confirm the"
                  " severity and keep or adjust the issue. Output the Finding"
                  " (echo the path)."
              ),
          ),
      ),
      Capability(
          name="triager",
          input_kind="list",
          output_model=ReportFixed,
          serialize_input=True,
          build=lambda: Agent(
              name="triager",
              model=MODEL,
              output_schema=ReportFixed,
              generate_content_config=DET,
              instruction=(
                  "Input: a JSON list of Findings. Output ReportFixed:"
                  " total, per-severity counts (must sum to total), and"
                  " a one-line summary."
              ),
          ),
      ),
      Capability(
          name="formatter",
          input_kind="item",
          output_model=Note,
          serialize_input=True,
          build=lambda: Agent(
              name="formatter",
              model=MODEL,
              output_schema=Note,
              generate_content_config=DET,
              instruction=(
                  "Input: a ReportFixed JSON. Output a Note: a one-line"
                  " markdown bullet summarizing the audit."
              ),
          ),
      ),
  ])


_REGISTRY_DESC = (
    "reviewer (item: a file with path and code -> Finding), verifier (item: a"
    " Finding -> a confirmed Finding), triager (LIST of Findings ->"
    " ReportFixed), formatter (item: a ReportFixed -> Note)."
)
_PLANNER_INSTR = (
    "Author a WorkflowSpec using ONLY these capabilities: "
    + _REGISTRY_DESC
    + " The task input has a 'files' list of objects with path and code."
    " Author, in order:"
    " (1) a pipeline over task.files with two stages, reviewer then verifier,"
    " so each file is reviewed and then its finding is verified per item;"
    " (2) a step running triager on the pipeline output;"
    " (3) a step running formatter on the report."
    " Use Binding(source='task', path='files') for the pipeline's over, and"
    " Binding(source='step', step=<id>) to chain steps. A pipeline stage takes"
    " its input from the previous stage automatically, so stages need no input"
    " binding. Set output to the formatter step."
)


# The QUALITY-GATE beat: a deliberately biased ask — the reviewer
# double-checking its OWN findings. Registry/bindings/shapes are all valid, so
# plain validation passes; only the plan-quality lints catch the structural
# self-review bias, and the demo rejects the plan before freezing or running.
_SLOPPY_TRIGGERS = ("sloppy", "self-review", "own findings", "double-check")
_SLOPPY_PLANNER_INSTR = (
    "Author a WorkflowSpec using ONLY these capabilities: "
    + _REGISTRY_DESC
    + " The task input has a 'files' list of objects with path and code."
    " Author, in order:"
    " (1) a pipeline over task.files with two stages, reviewer then reviewer"
    " AGAIN — the reviewer double-checks its own findings per item;"
    " (2) a step running triager on the pipeline output;"
    " (3) a step running formatter on the report."
    " Use Binding(source='task', path='files') for the pipeline's over, and"
    " Binding(source='step', step=<id>) to chain steps. A pipeline stage takes"
    " its input from the previous stage automatically, so stages need no input"
    " binding. Set output to the formatter step."
)


# The FREE-AUTHORING beat: the planner receives ONLY the goal + capability
# descriptions — no plan recipe. This is the honest "model-authored" claim
# (the default _PLANNER_INSTR dictates the shape for recording reliability;
# the spike's demand gate also used free authoring).
_FREE_TRIGGERS = ("freely", "free-form", "your own plan", "decompose")
_FREE_PLANNER_INSTR = (
    "Author a WorkflowSpec using ONLY these capabilities: "
    + _REGISTRY_DESC
    + " The task input has a 'files' list of objects with path and code."
    " GOAL: audit the files for security issues and produce a one-line"
    " report note. Decompose the goal into a plan YOURSELF — no recipe is"
    " provided. Choose whichever control blocks fit (step / fan_out /"
    " pipeline / branch / loop_until). Binding rules:"
    " Binding(source='task', path='files') reads the file list;"
    " Binding(source='step', step=<id>) reads a prior step's output; a"
    " pipeline stage takes the previous stage's per-item output"
    " automatically. Set output to the final step."
)


def _msg(text: str) -> Event:
  return Event(
      content=types.Content(role="model", parts=[types.Part(text=text)])
  )


def _hash(spec: WorkflowSpec) -> str:
  # The one canonical hash definition (authored_workflow_spike/authoring.py),
  # shown truncated; the full digest lives in the exported FrozenWorkflowRecord.
  return sha256_hex(spec.model_dump(mode="json"))[:12]


# Where the "Export plan" beat writes the portable envelope (cwd of `adk web`).
_EXPORT_PATH = os.path.join(os.getcwd(), "security_audit_plan.json")


@node(rerun_on_resume=True)
async def author_validate_execute(ctx: Context, node_input):
  reg = _registry()

  # 0. QUALITY-GATE path (checked before load-or-author so it works in any
  # session): an adversarial ask makes the planner author a structurally
  # biased plan; the lints catch it and the gate rejects it pre-execution.
  if any(k in str(node_input or "").lower() for k in _SLOPPY_TRIGGERS):
    yield _msg(
        "🧭 **Adversarial ask** — authoring a plan where the reviewer"
        " double-checks its OWN findings. Watch the quality gate."
    )
    sloppy = Agent(
        name="planner",
        model=MODEL,
        output_schema=WorkflowSpec,
        generate_content_config=DET,
        instruction=_SLOPPY_PLANNER_INSTR,
    )
    raw = await ctx.run_node(
        sloppy,
        node_input=f"Audit these files: {[f['path'] for f in FILES]}.",
        run_id="plan_sloppy",
    )
    spec = WorkflowSpec.model_validate(raw)
    yield _msg(
        "📋 **Authored plan** (valid registry refs, valid bindings, valid"
        f" shapes):\n```json\n{json.dumps(spec.model_dump(), indent=1)}\n```"
    )
    lints = [
        w
        for w in WorkflowSpecValidator(reg).validate(spec)
        if w.startswith("plan-quality")
    ]
    if lints:
      fired = "\n".join(f"   - ⚠️ {w}" for w in lints)
      yield _msg(
          f"🚨 **Plan-quality lints fired ({len(lints)}):**\n{fired}\n\n🛑"
          " **Plan rejected by the quality gate** — NOT frozen, NOT executed."
          " Plain validation passed (every capability is registered, every"
          " binding is typed); only the structural bias check caught it. In"
          " production this triggers a bounded re-plan (`max_replans`)."
      )
    else:
      yield _msg(
          "ℹ️ The planner did not author the biased shape this time —"
          " re-send the prompt to retry the adversarial ask."
      )
    yield Event(output={"rejected": bool(lints), "lints": len(lints)})
    return

  # 1. LOAD-OR-AUTHOR. If a frozen spec exists in this session, REUSE it (do not
  # re-author) — this is the resume/reproducibility claim. Otherwise the model
  # authors a fresh typed WorkflowSpec (data, not code).
  existing = ctx.state.get("authored_workflow:frozen_spec")
  if existing:
    spec = WorkflowSpec.model_validate(existing)
    spec_hash = ctx.state.get("authored_workflow:frozen_spec_hash") or _hash(
        spec
    )
    reused = True
    yield _msg(
        f"♻️ **Reusing frozen plan** from session state — hash `{spec_hash}`. "
        "The model is NOT re-invoked; the exact prior plan is replayed."
    )
  else:
    reused = False
    free = any(k in str(node_input or "").lower() for k in _FREE_TRIGGERS)
    cap_list = ", ".join(f"`{n}`" for n in reg.names())
    if free:
      yield _msg(
          "🧭 **Free authoring** — the planner receives ONLY the goal +"
          f" capability descriptions ({cap_list}); no plan recipe. The shape"
          " below is the model's own decomposition (it may differ run to"
          " run — and the freeze beat then makes THIS run replayable)."
      )
    else:
      yield _msg(
          "🧭 **Model-authored Workflow** — planning a security audit over "
          f"{len(FILES)} files using only registered capabilities "
          f"({cap_list})."
      )
    planner = Agent(
        name="planner",
        model=MODEL,
        output_schema=WorkflowSpec,
        generate_content_config=DET,
        instruction=_FREE_PLANNER_INSTR if free else _PLANNER_INSTR,
    )
    raw = await ctx.run_node(
        planner,
        node_input=f"Audit these files: {[f['path'] for f in FILES]}.",
        run_id="plan",
    )
    spec = WorkflowSpec.model_validate(raw)
    spec_hash = _hash(spec)
    steps = " → ".join(s.kind for s in spec.steps)
    yield _msg(
        f"📋 **Authored plan** (`{steps}`):\n```json\n"
        f"{json.dumps(spec.model_dump(), indent=1)}\n```"
    )

  # 2. VALIDATE — semantic validation against the registry (always).
  warnings = WorkflowSpecValidator(reg).validate(spec)  # raises on hard error
  caps = set()
  for s in spec.steps:
    if getattr(s, "capability", None):
      caps.add(s.capability)
    for st in getattr(s, "stages", None) or []:  # pipeline stage capabilities
      caps.add(st.capability)
  caps = sorted(caps)
  yield _msg(
      "✅ **Validation passed.** Capabilities referenced (all registered): "
      f"`{caps}`."
      + (f"\n⚠️ warnings: {warnings}" if warnings else "")
  )

  # 2b. INDEPENDENCE — the quality argument, made static. Isolation is what
  # mitigates self-preferential bias and goal drift in multi-agent work; with
  # typed bindings it is a checkable property of the frozen plan (the validator
  # lints same-capability self-review and unsynthesized fan-out), not a runtime
  # hope. Model-authored orchestration *code* cannot be checked this way.
  lints = [w for w in warnings if w.startswith("plan-quality")]
  facts = "\n".join(f"   - {f}" for f in independence_facts(spec))
  yield _msg(
      f"🧪 **Plan-quality lints: {len(lints)} warnings.** Agent independence"
      " is statically checkable from the typed bindings — the frozen record"
      f" *proves* it to an auditor:\n{facts}"
  )

  # 3. FREEZE — persist spec + hash to session state on first author only
  # (visible in the State tab; reused runs already have it).
  # NOTE: session state keeps a minimal {spec, hash} subset so the State tab
  # stays readable for the resume/reuse beat. The EXPORT beat below serializes
  # the full FrozenWorkflowRecord (planner/registry/capability versions,
  # validation, task_input_digest) — see authored_workflow_spike/DESIGN.md §5/§10.
  # Production v1 would persist that full record to state too; the split here is
  # presentational, not the canonical contract.
  if not reused:
    ctx.state["authored_workflow:frozen_spec"] = spec.model_dump()
    ctx.state["authored_workflow:frozen_spec_hash"] = spec_hash
    yield _msg(
        f"🔒 **Frozen spec** persisted to session state — hash `{spec_hash}`. "
        "Re-send the prompt: it replays this exact plan, not a new one."
    )

    # 3b. EXPORT — serialize the full FrozenWorkflowRecord to a portable JSON
    # envelope (DESIGN.md §10), then prove the import contract by re-importing
    # it: import_plan recomputes the hash and re-validates against the CURRENT
    # registry — it never trusts the envelope's own `validation`.
    record = FrozenWorkflowRecord.freeze(
        spec,
        planner_model=MODEL,
        registry=reg,
        created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        task_input={"files": FILES},
    )
    envelope = export_plan(record)
    try:
      with open(_EXPORT_PATH, "w") as f:
        json.dump(envelope, f, indent=2)
      import_plan(
          envelope, reg, task_input={"files": FILES}
      )  # re-hash+re-validate
      yield _msg(
          f"📦 **Exported plan** → `{os.path.basename(_EXPORT_PATH)}` "
          f"(full `{record.spec_hash[:12]}`, schema `{record.schema_version}`, "
          f"planner `{record.planner_model}`). Re-imported OK — import "
          "recomputes the hash and re-validates against the current registry, "
          "never trusting the envelope's own validation. This is the "
          "reviewable / diffable / replayable audit artifact."
      )
    except OSError as e:
      yield _msg(f"📦 Export skipped (filesystem): {e}")

    # 3c. LOWER — project the plan's STATIC subset toward ADK config shapes
    # (RFC #93 §11 convergence, shown concretely). Illustrative structural
    # projection — NOT a loadable root_agent.yaml: leaves are referenced by
    # allow-listed capability name (never an importable FQN), and dynamic blocks
    # (pipeline/fan_out/branch) are flagged unsupported, never fabricated.
    cov = agent_config_coverage(spec)
    lowered = lower_to_agent_config(spec, name="security_audit_planner")
    yield _msg(
        "🧬 **ADK config lowering (static subset)** —"
        f" {cov['lowerable']}/{cov['total']} top-level steps project to ADK"
        " config; dynamic blocks stay SpecInterpreter-only:"
        f" {cov['dynamic']}.\n```json\n{json.dumps(lowered, indent=1)}\n```"
    )

  # 4. EXECUTE — run the validated plan on the real ADK engine (#92 supervisor).
  t0 = time.perf_counter()
  interp = SpecInterpreter(reg, ctx)
  result = await interp.execute(spec, {"files": FILES})
  elapsed = time.perf_counter() - t0
  yield _msg(
      "📄 **Audit result:**"
      f" {result.get('note') if isinstance(result, dict) else result}"
  )
  # 4b. COST — cheap visibility into what the orchestration spent. The planner
  # was invoked at most once (zero on frozen replay); every capability dispatch
  # ran OUTSIDE the planner's context.
  planner_cost = (
      "0 planner calls (frozen replay)" if reused else "1 planner call"
  )
  yield _msg(
      f"📊 **Cost:** {interp.dispatch_count} capability dispatches in"
      f" {elapsed:.1f}s + {planner_cost} — per-step work runs outside the"
      " planner's context."
  )
  yield Event(
      output={
          "hash": spec_hash,
          "result": result,
          "capabilities": caps,
          "reused": reused,
          "dispatches": interp.dispatch_count,
      }
  )


root_agent = Workflow(
    name="security_audit_planner",
    edges=[("START", author_validate_execute)],
)
