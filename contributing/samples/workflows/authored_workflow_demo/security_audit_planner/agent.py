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

import hashlib
import json
import os
import sys
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
from authoring import Capability  # noqa: E402
from authoring import CapabilityRegistry  # noqa: E402
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


def _msg(text: str) -> Event:
  return Event(
      content=types.Content(role="model", parts=[types.Part(text=text)])
  )


def _hash(spec: WorkflowSpec) -> str:
  return hashlib.sha256(
      json.dumps(spec.model_dump(), sort_keys=True).encode()
  ).hexdigest()[:12]


@node(rerun_on_resume=True)
async def author_validate_execute(ctx: Context, node_input):
  reg = _registry()

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
    yield _msg(
        "🧭 **Model-authored Workflow** — planning a security audit over "
        f"{len(FILES)} files using only registered capabilities "
        "(`reviewer`, `triager`, `formatter`)."
    )
    planner = Agent(
        name="planner",
        model=MODEL,
        output_schema=WorkflowSpec,
        generate_content_config=DET,
        instruction=_PLANNER_INSTR,
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

  # 3. FREEZE — persist spec + hash to session state on first author only
  # (visible in the State tab; reused runs already have it).
  # NOTE: this demo persists only a minimal {spec, hash} subset to keep the
  # walkthrough readable. Production v1 would store the full FrozenWorkflowRecord
  # (planner/registry/capability versions, validation, task_input_schema/digest)
  # — see authored_workflow_spike/DESIGN.md §5. The demo is illustrative, not the
  # canonical persistence contract.
  if not reused:
    ctx.state["authored_workflow:frozen_spec"] = spec.model_dump()
    ctx.state["authored_workflow:frozen_spec_hash"] = spec_hash
    yield _msg(
        f"🔒 **Frozen spec** persisted to session state — hash `{spec_hash}`. "
        "Re-send the prompt: it replays this exact plan, not a new one."
    )

  # 4. EXECUTE — run the validated plan on the real ADK engine (#92 supervisor).
  result = await SpecInterpreter(reg, ctx).execute(spec, {"files": FILES})
  yield _msg(
      "📄 **Audit result:**"
      f" {result.get('note') if isinstance(result, dict) else result}"
  )
  yield Event(
      output={
          "hash": spec_hash,
          "result": result,
          "capabilities": caps,
          "reused": reused,
      }
  )


root_agent = Workflow(
    name="security_audit_planner",
    edges=[("START", author_validate_execute)],
)
