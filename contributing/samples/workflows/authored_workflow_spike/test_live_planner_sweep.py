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

"""OPTIONAL live planner sweep for RFC #93 — coverage across plan shapes.

Skipped unless a real model is configured (no hardcoded project/model). Asks a
planner LlmAgent(output_schema=WorkflowSpec) to author plans for three shapes —
multi-stage, branch, loop_until — then validates and executes each on the real
ADK engine. Demonstrates authoring quality beyond the single fan-out/aggregate
shape from the original gate.

Enable (Vertex):
    export SPIKE_LIVE=1 GOOGLE_GENAI_USE_VERTEXAI=1
    export GOOGLE_CLOUD_PROJECT=<project> GOOGLE_CLOUD_LOCATION=global
    export SPIKE_GEMINI_MODEL=gemini-3.5-flash   # 3.5 serves from `global`
    pytest test_live_planner_sweep.py -q -s
"""

from __future__ import annotations

import os
import sys
from typing import Literal

from google.adk import Agent
from google.adk import Event
from google.adk import Workflow
from google.adk.runners import Runner
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.adk.workflow import node
from google.genai import types
from pydantic import BaseModel
import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from authoring import Capability  # noqa: E402
from authoring import CapabilityRegistry
from authoring import SpecInterpreter
from authoring import WorkflowSpec
from authoring import WorkflowSpecValidator

_LIVE = os.environ.get("SPIKE_LIVE") == "1" and bool(
    os.environ.get("GOOGLE_CLOUD_PROJECT")
)
pytestmark = pytest.mark.skipif(
    not _LIVE, reason="set SPIKE_LIVE=1 + project/model env to run"
)
MODEL = os.environ.get("SPIKE_GEMINI_MODEL", "gemini-2.5-flash")
DET = types.GenerateContentConfig(temperature=0)


def _agent(name, schema, instr):
  return Capability(
      name=name,
      input_kind="item",
      output_model=schema,
      serialize_input=True,
      build=lambda: Agent(
          name=name,
          model=MODEL,
          output_schema=schema,
          generate_content_config=DET,
          instruction=instr,
      ),
  )


# Enumerated fields (NOT an open dict) — the contract lesson from the first gate.
class ReportFixed(BaseModel):
  total: int
  critical: int
  high: int
  medium: int
  low: int
  none: int
  summary: str


class Finding(BaseModel):
  path: str
  severity: Literal["CRITICAL", "HIGH", "MEDIUM", "LOW", "NONE"]
  issue: str


class Verdict(BaseModel):
  is_tech: bool


class Category(BaseModel):
  category: Literal["tech", "other"]


class Note(BaseModel):
  note: str


def _registry():
  caps = [
      _agent(
          "reviewer",
          Finding,
          "Input JSON with keys path and code. Output a Finding (echo path).",
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
                  "Input: JSON list of Findings. Output ReportFixed: total, "
                  "per-severity counts (sum to total), one-line summary."
              ),
          ),
      ),
      _agent(
          "formatter",
          Note,
          "Input: a ReportFixed JSON. Output a Note: a one-line markdown"
          " bullet.",
      ),
      _agent(
          "writer",
          Note,
          "Input: a topic (maybe with feedback). Output a Note: a short tech"
          " headline.",
      ),
      _agent(
          "is_tech",
          Verdict,
          "Input: a headline/Note JSON. Output Verdict.is_tech=true iff it is"
          " about technology/software.",
      ),
      _agent(
          "classifier",
          Category,
          "Input: a short text. Output Category 'tech' or 'other'.",
      ),
      _agent(
          "tech_note",
          Note,
          "Input: text. Output a Note summarizing it as a tech item.",
      ),
      _agent(
          "other_note",
          Note,
          "Input: text. Output a Note summarizing it as a general item.",
      ),
  ]
  # mark reviewer as item/list correctly
  caps[0] = Capability(
      name="reviewer",
      input_kind="item",
      output_model=Finding,
      serialize_input=True,
      build=lambda: Agent(
          name="reviewer",
          model=MODEL,
          output_schema=Finding,
          generate_content_config=DET,
          instruction=(
              "Input JSON with keys path and code. Output a Finding (echo"
              " path)."
          ),
      ),
  )
  return CapabilityRegistry(caps)


SHAPES = {
    "multi_stage": {
        "registry_desc": (
            "reviewer (item: a file with path and code -> Finding), triager"
            " (LIST of Findings -> ReportFixed), formatter (item: a ReportFixed"
            " -> Note)."
        ),
        "task": (
            "Audit files for security. Fan out reviewer over task.files (a list"
            " of {path,code}), triager on the findings, then formatter on the"
            " report. output=formatter."
        ),
        "task_input": {
            "files": [
                {"path": "a.py", "code": "os.system('ping '+host)"},
                {"path": "b.py", "code": "def add(x,y): return x+y"},
            ]
        },
    },
    "branch": {
        "registry_desc": (
            "classifier (item: text -> Category with category tech or other),"
            " tech_note (item -> Note), other_note (item -> Note)."
        ),
        "task": (
            "Classify task.text with classifier, then branch on the category."
            " The classifier outputs a Category object, so bind the branch `on`"
            " to its category field (Binding source=step, step=<classifier id>,"
            " path='category'). Routes: tech->tech_note, other->other_note"
            " (both run on task.text). output=the branch."
        ),
        "task_input": {"text": "a new programming language for systems code"},
    },
    "loop": {
        "registry_desc": (
            "writer (item: a topic -> a Note headline), is_tech (item: a Note"
            " -> a Verdict with boolean is_tech)."
        ),
        "task": (
            "loop_until: body=[writer on task.topic], until_capability=is_tech"
            " with until_input bound to the writer step, max_iters=3."
            " output=the loop."
        ),
        "task_input": {"topic": "quantum computing"},
    },
}


async def _author_validate_execute(shape, cfg):
  reg = _registry()
  planner = Agent(
      name="planner",
      model=MODEL,
      output_schema=WorkflowSpec,
      generate_content_config=DET,
      instruction=(
          "Author a WorkflowSpec using ONLY these capabilities: "
          + cfg["registry_desc"]
          + " Use Binding(source='task', path=...) for task input and"
          " Binding(source='step', step=<id>) to chain. "
          + cfg["task"]
      ),
  )
  holder = {}

  @node(rerun_on_resume=True)
  async def parent(ctx, node_input):
    raw = await ctx.run_node(
        planner, node_input=f"Shape: {shape}. Author the plan.", run_id="plan"
    )
    spec = WorkflowSpec.model_validate(raw)
    holder["spec"] = spec
    WorkflowSpecValidator(reg).validate(spec)  # raises on invalid
    holder["valid"] = True
    interp = SpecInterpreter(reg, ctx)
    holder["output"] = await interp.execute(spec, cfg["task_input"])
    yield Event(output={"_done": True})

  wf = Workflow(name=shape, edges=[("START", parent)])
  ss = InMemorySessionService()
  r = Runner(app_name=wf.name, node=wf, session_service=ss)
  s = await ss.create_session(app_name=wf.name, user_id="u")
  async for _ in r.run_async(
      user_id="u",
      session_id=s.id,
      new_message=types.Content(parts=[types.Part(text="go")], role="user"),
  ):
    pass
  return holder


def _all_capabilities(nodes) -> set:
  """Capabilities referenced anywhere in the plan tree (incl. branch/loop)."""
  out = set()
  for n in nodes:
    if n.kind in ("step", "fan_out"):
      out.add(n.capability)
    elif n.kind == "loop_until":
      out.add(n.until_capability)
      out |= _all_capabilities(n.body)
    elif n.kind == "branch":
      for route in n.routes:
        out |= _all_capabilities(route.block)
  return out


@pytest.mark.parametrize("shape", list(SHAPES))
@pytest.mark.asyncio
async def test_planner_sweep(shape):
  h = await _author_validate_execute(shape, SHAPES[shape])
  spec = h["spec"]
  top_kinds = [s.kind for s in spec.steps]
  caps = _all_capabilities(spec.steps)
  print(
      f"\n[{shape}] top_kinds={top_kinds} caps={sorted(caps)} "
      f"valid={h.get('valid')} output={str(h.get('output'))[:100]}"
  )
  assert h.get("valid") is True
  assert h.get("output") is not None

  # Shape-specific structure — a degenerate plan must NOT pass.
  if shape == "multi_stage":
    assert top_kinds == ["fan_out", "step", "step"]
    assert {"reviewer", "triager", "formatter"} <= caps
  elif shape == "branch":
    assert "branch" in top_kinds and "step" in top_kinds
    branch = next(n for n in spec.steps if n.kind == "branch")
    route_values = {r.value for r in branch.routes}
    route_caps = set()
    for r in branch.routes:
      route_caps |= _all_capabilities(r.block)
    assert route_values == {
        "tech",
        "other",
    }, route_values  # exact routes, not just >=2
    assert {
        "tech_note",
        "other_note",
    } <= route_caps  # both routes wired correctly
    assert "classifier" in caps
  elif shape == "loop":
    loop = next(n for n in spec.steps if n.kind == "loop_until")
    assert len(loop.body) >= 1
    assert "writer" in caps and "is_tech" in caps
