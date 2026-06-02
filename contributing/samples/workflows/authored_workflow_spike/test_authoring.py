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

"""Deterministic, CI-safe tests for the authored-workflow spike (RFC #93).

No LLM. Capabilities are deterministic stub nodes, so these exercise the
validator + the interpreter (step / fan_out / pipeline / branch / loop_until + binding
scope) on the real ADK Workflow engine. The live planner sweep lives in
test_live_planner_sweep.py (env-gated).
"""

from __future__ import annotations

import os
import sys

from google.adk import Event
from google.adk import Workflow
from google.adk.runners import Runner
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.adk.workflow import node
from google.genai import types
from pydantic import BaseModel
import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from authoring import Binding  # noqa: E402
from authoring import Branch
from authoring import Capability
from authoring import CapabilityRegistry
from authoring import FanOut
from authoring import LoopUntil
from authoring import Pipeline
from authoring import PipelineStage
from authoring import Route
from authoring import SpecInterpreter
from authoring import SpecValidationError
from authoring import StepRef
from authoring import WorkflowSpec
from authoring import WorkflowSpecValidator


# ----------------------------------------------------------------- stub caps
def _cap_node(name, fn):
  def build():
    @node(name=name)
    async def n(ctx, node_input):
      yield Event(output=fn(node_input))

    return n

  return build


def _registry():
  return CapabilityRegistry([
      Capability(
          name="review",
          build=_cap_node(
              "review",
              lambda f: {
                  "path": f["path"],
                  "severity": "HIGH" if "bad" in f["code"] else "NONE",
              },
          ),
          input_kind="item",
          serialize_input=False,
          max_fan_out=10,
      ),
      Capability(
          name="count",
          build=_cap_node(
              "count",
              lambda findings: {
                  "n": len(findings),
                  "high": sum(1 for x in findings if x["severity"] == "HIGH"),
              },
          ),
          input_kind="list",
          serialize_input=False,
      ),
      Capability(
          name="classify",
          build=_cap_node(
              "classify", lambda s: "tech" if "code" in str(s) else "other"
          ),
          input_kind="item",
          serialize_input=False,
      ),
      Capability(
          name="tech_summary",
          build=_cap_node("tech_summary", lambda s: "TECH:" + str(s)),
          input_kind="item",
          serialize_input=False,
      ),
      Capability(
          name="other_summary",
          build=_cap_node("other_summary", lambda s: "OTHER:" + str(s)),
          input_kind="item",
          serialize_input=False,
      ),
      Capability(
          name="draft",
          build=_cap_node("draft", lambda s: {"text": "v", "len": len(str(s))}),
          input_kind="item",
          serialize_input=False,
      ),
      Capability(
          name="is_good",
          build=_cap_node("is_good", lambda s: True),
          input_kind="item",
          serialize_input=False,
      ),
  ])


async def _run_spec(spec, registry, task_input):
  holder = {}

  @node(rerun_on_resume=True)
  async def parent(ctx, node_input):
    interp = SpecInterpreter(registry, ctx)
    holder["out"] = await interp.execute(spec, task_input)
    yield Event(output={"_done": True})

  wf = Workflow(name="t", edges=[("START", parent)])
  ss = InMemorySessionService()
  r = Runner(app_name=wf.name, node=wf, session_service=ss)
  s = await ss.create_session(app_name=wf.name, user_id="u")
  async for _ in r.run_async(
      user_id="u",
      session_id=s.id,
      new_message=types.Content(parts=[types.Part(text="go")], role="user"),
  ):
    pass
  return holder["out"]


# ----------------------------------------------------------------- validator
def test_binding_invariant():
  with pytest.raises(Exception):
    Binding(source="step")  # step missing
  with pytest.raises(Exception):
    Binding(source="task", step="x")  # step set for task


def test_loop_max_iters_must_be_positive():
  with pytest.raises(Exception):
    LoopUntil(
        kind="loop_until",
        id="l",
        body=[],
        until_capability="is_good",
        until_input=Binding(source="task"),
        max_iters=0,
    )


def _fanout_aggregate_spec():
  return WorkflowSpec(
      goal="audit",
      steps=[
          FanOut(
              kind="fan_out",
              id="rev",
              over=Binding(source="task", path="files"),
              capability="review",
          ),
          StepRef(
              kind="step",
              id="agg",
              capability="count",
              input=Binding(source="step", step="rev"),
          ),
      ],
      output=Binding(source="step", step="agg"),
  )


def test_validator_accepts_valid_spec():
  WorkflowSpecValidator(_registry()).validate(
      _fanout_aggregate_spec()
  )  # no raise


def test_validator_rejects_unknown_capability():
  spec = _fanout_aggregate_spec()
  spec.steps[0].capability = "nope"
  with pytest.raises(SpecValidationError):
    WorkflowSpecValidator(_registry()).validate(spec)


def test_validator_rejects_nonpreceding_binding():
  spec = WorkflowSpec(
      goal="x",
      steps=[
          StepRef(
              kind="step",
              id="a",
              capability="count",
              input=Binding(source="step", step="later"),
          )
      ],  # references a later/unknown step
      output=Binding(source="step", step="a"),
  )
  with pytest.raises(SpecValidationError):
    WorkflowSpecValidator(_registry()).validate(spec)


def test_validator_rejects_duplicate_id():
  spec = WorkflowSpec(
      goal="x",
      steps=[
          StepRef(
              kind="step",
              id="a",
              capability="classify",
              input=Binding(source="task"),
          ),
          StepRef(
              kind="step",
              id="a",
              capability="classify",
              input=Binding(source="task"),
          ),
      ],
      output=Binding(source="step", step="a"),
  )
  with pytest.raises(SpecValidationError):
    WorkflowSpecValidator(_registry()).validate(spec)


def test_open_map_warning():
  class BadReport(BaseModel):
    total: int
    counts: dict[str, int]  # open map — should warn

  reg = CapabilityRegistry([
      Capability(
          name="triage",
          build=lambda: None,
          input_kind="list",
          output_model=BadReport,
      )
  ])
  warnings = reg.open_map_warnings()
  assert any("open map" in w for w in warnings)


# ----------------------------------------------------------------- interpreter
@pytest.mark.asyncio
async def test_interpreter_fanout_then_aggregate():
  files = [
      {"path": "a.py", "code": "bad thing"},
      {"path": "b.py", "code": "fine"},
      {"path": "c.py", "code": "bad"},
  ]
  out = await _run_spec(_fanout_aggregate_spec(), _registry(), {"files": files})
  assert out == {"n": 3, "high": 2}


@pytest.mark.asyncio
async def test_interpreter_branch_takes_correct_route():
  spec = WorkflowSpec(
      goal="branch",
      steps=[
          StepRef(
              kind="step",
              id="cls",
              capability="classify",
              input=Binding(source="task"),
          ),
          Branch(
              kind="branch",
              id="br",
              on=Binding(source="step", step="cls"),
              routes=[
                  Route(
                      value="tech",
                      block=[
                          StepRef(
                              kind="step",
                              id="t",
                              capability="tech_summary",
                              input=Binding(source="task"),
                          )
                      ],
                  ),
                  Route(
                      value="other",
                      block=[
                          StepRef(
                              kind="step",
                              id="o",
                              capability="other_summary",
                              input=Binding(source="task"),
                          )
                      ],
                  ),
              ],
          ),
      ],
      output=Binding(source="step", step="br"),
  )
  WorkflowSpecValidator(_registry()).validate(spec)
  assert (await _run_spec(spec, _registry(), "this is code")).startswith(
      "TECH:"
  )
  assert (await _run_spec(spec, _registry(), "hello world")).startswith(
      "OTHER:"
  )


@pytest.mark.asyncio
async def test_interpreter_loop_until_stops_and_outputs():
  spec = WorkflowSpec(
      goal="loop",
      steps=[
          LoopUntil(
              kind="loop_until",
              id="lp",
              body=[
                  StepRef(
                      kind="step",
                      id="d",
                      capability="draft",
                      input=Binding(source="task"),
                  )
              ],
              until_capability="is_good",
              until_input=Binding(source="step", step="d"),
              max_iters=3,
          ),
      ],
      output=Binding(source="step", step="lp"),
  )
  WorkflowSpecValidator(_registry()).validate(spec)
  out = await _run_spec(spec, _registry(), "topic")
  assert out == {
      "text": "v",
      "len": len("topic"),
  }  # loop output = last body node output


# ----------------------------------------------------------------- pipeline
def _timed_registry(log):
  """reviewer (stage 0) + verifier (stage 1) as deterministic timed stubs."""
  import asyncio
  import time

  def stage_cap(name, slow_for=None, key="r"):
    def build():
      @node(name=name)
      async def n(ctx, node_input):
        item = node_input
        log.append((name, "start", time.perf_counter()))
        await asyncio.sleep(
            0.05 if (slow_for is not None and item == slow_for) else 0.0
        )
        log.append((name, "end", time.perf_counter()))
        yield Event(output={key: item})

      return n

    return Capability(
        name=name, build=build, input_kind="item", serialize_input=False
    )

  return CapabilityRegistry([
      stage_cap("reviewer", slow_for=1, key="review"),
      stage_cap("verifier", key="verdict"),
  ])


def _pipeline_spec():
  return WorkflowSpec(
      goal="pipe",
      steps=[
          Pipeline(
              kind="pipeline",
              id="pp",
              over=Binding(source="task", path="items"),
              stages=[
                  PipelineStage(capability="reviewer"),
                  PipelineStage(capability="verifier"),
              ],
          )
      ],
      output=Binding(source="step", step="pp"),
  )


def test_validator_accepts_pipeline():
  log = []
  WorkflowSpecValidator(_timed_registry(log)).validate(_pipeline_spec())


def test_validator_rejects_pipeline_list_stage():
  spec = _pipeline_spec()
  # "count" takes a list, not an item -> invalid pipeline stage
  spec.steps[0].stages[1] = PipelineStage(capability="count")
  with pytest.raises(SpecValidationError):
    WorkflowSpecValidator(_registry()).validate(spec)


@pytest.mark.asyncio
async def test_interpreter_pipeline_ordered_and_barrier_free():
  log = []
  reg = _timed_registry(log)
  # input items [0, 1]; reviewer is slow for item 1 only.
  out = await _run_spec(_pipeline_spec(), reg, {"items": [0, 1]})

  # Ordered, per-item review->verify (verdict carries the reviewed value):
  assert out == [{"verdict": {"review": 0}}, {"verdict": {"review": 1}}]

  starts = {n: t for (n, p, t) in log if p == "start"}
  ends = {n: t for (n, p, t) in log if p == "end"}
  # BARRIER-FREE proof: item 0 reaches stage 2 (verifier) BEFORE item 1 finishes
  # stage 1 (reviewer). Two barriered fan_outs could NOT do this — every
  # reviewer would finish before any verifier started.
  assert "verifier" in starts and "reviewer" in ends
  # earliest verifier start precedes the latest reviewer end:
  first_verifier_start = min(
      t for (n, p, t) in log if n == "verifier" and p == "start"
  )
  last_reviewer_end = max(
      t for (n, p, t) in log if n == "reviewer" and p == "end"
  )
  assert first_verifier_start < last_reviewer_end


@pytest.mark.asyncio
async def test_interpreter_pipeline_enforces_max_fan_out():
  # Each stage dispatches once per item, so a stage capability's max_fan_out is
  # a data-dependent cap that must be enforced at runtime (same as FanOut).
  log = []
  reg = _timed_registry(log)
  reg["verifier"].max_fan_out = 1  # 2 items > cap -> reject before dispatch
  with pytest.raises(SpecValidationError):
    await _run_spec(_pipeline_spec(), reg, {"items": [0, 1]})
  # rejected pre-dispatch: no stage ran.
  assert log == []
