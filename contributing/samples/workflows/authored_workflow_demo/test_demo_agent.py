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

"""CI-safe tests for the ADK Web demo wrapper (no live model).

Covers: import + `root_agent` shape, the demo registry, demo-spec validation,
and — crucially — the **reuse path** end-to-end with a stubbed (deterministic)
capability registry, so the frozen-spec/replay claim the demo makes on camera
is actually verified without calling Gemini.
"""

from __future__ import annotations

import json
import os
import sys

from google.adk import Event
from google.adk.runners import Runner
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.adk.workflow import node
from google.adk.workflow import Workflow
from google.genai import types
import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "security_audit_planner"))
sys.path.insert(0, os.path.join(_HERE, "..", "authored_workflow_spike"))
import agent as demo  # noqa: E402
from authoring import Binding  # noqa: E402
from authoring import Capability  # noqa: E402
from authoring import CapabilityRegistry  # noqa: E402
from authoring import Pipeline  # noqa: E402
from authoring import PipelineStage  # noqa: E402
from authoring import StepRef  # noqa: E402
from authoring import WorkflowSpec  # noqa: E402
from authoring import WorkflowSpecValidator  # noqa: E402


def _demo_spec() -> WorkflowSpec:
  return WorkflowSpec(
      goal="audit",
      steps=[
          Pipeline(
              kind="pipeline",
              id="rev",
              over=Binding(source="task", path="files"),
              stages=[
                  PipelineStage(capability="reviewer"),
                  PipelineStage(capability="verifier"),
              ],
          ),
          StepRef(
              kind="step",
              id="tri",
              capability="triager",
              input=Binding(source="step", step="rev"),
          ),
          StepRef(
              kind="step",
              id="fmt",
              capability="formatter",
              input=Binding(source="step", step="tri"),
          ),
      ],
      output=Binding(source="step", step="fmt"),
  )


def test_root_agent_importable_and_named():
  assert isinstance(demo.root_agent, Workflow)
  assert demo.root_agent.name == "security_audit_planner"
  assert len(demo.root_agent.edges) == 1


def test_demo_registry_is_clean():
  reg = demo._registry()
  for name in ("reviewer", "verifier", "triager", "formatter"):
    assert name in reg
  assert reg["reviewer"].input_kind == "item"
  assert reg["verifier"].input_kind == "item"  # pipeline stages take an item
  assert reg["triager"].input_kind == "list"
  # ReportFixed uses enumerated fields, not an open dict[str, X] map.
  assert reg.open_map_warnings() == []


def test_demo_spec_validates():
  WorkflowSpecValidator(demo._registry()).validate(_demo_spec())  # no raise


def test_demo_spec_quality_lints_clean_and_independent():
  # Zero plan-quality lints: verification is by a DIFFERENT capability
  # (reviewer -> verifier), and the fan-out is synthesized (triager).
  warnings = WorkflowSpecValidator(demo._registry()).validate(_demo_spec())
  assert [w for w in warnings if w.startswith("plan-quality")] == []
  # The independence facts the demo shows on camera are derivable statically:
  # the verifier stage provably sees only the reviewer's per-item output, and
  # each downstream step provably consumes only its upstream's typed output.
  from authoring import independence_facts  # noqa: E402

  facts = "\n".join(independence_facts(_demo_spec()))
  assert "stage 'verifier' sees ONLY stage 'reviewer'" in facts
  assert "'tri' consumes ONLY the typed output of 'rev'" in facts
  assert "'fmt' consumes ONLY the typed output of 'tri'" in facts


def test_demo_spec_agentconfig_lowering():
  # The demo's plan (pipeline -> step -> step) is exactly the static/dynamic
  # split RFC #93 §11 describes: the two trailing steps lower to LlmAgent under
  # a SequentialAgent; the reviewer->verifier pipeline has no AgentConfig
  # equivalent. (Illustrative projection — leaves by capability name, not FQN.)
  from authoring import agent_config_coverage  # noqa: E402
  from authoring import lower_to_agent_config  # noqa: E402

  cfg = lower_to_agent_config(_demo_spec(), name="security_audit_planner")
  assert cfg["agent_class"] == "SequentialAgent"
  kinds = [s["agent_class"] for s in cfg["sub_agents"]]
  assert kinds == ["<no-AgentConfig-equivalent>", "LlmAgent", "LlmAgent"]
  assert agent_config_coverage(_demo_spec()) == {
      "total": 3,
      "lowerable": 2,
      "dynamic": ["pipeline"],
  }
  assert '"code"' not in json.dumps(cfg)  # never an importable FQN


def _stub_registry() -> CapabilityRegistry:
  def stub(name, fn):
    def build():
      @node(name=name)
      async def n(ctx, node_input):
        yield Event(output=fn(node_input))

      return n

    return build

  return CapabilityRegistry([
      Capability(
          name="reviewer",
          input_kind="item",
          serialize_input=False,
          build=stub(
              "reviewer",
              lambda f: {"path": f["path"], "severity": "HIGH", "issue": "x"},
          ),
      ),
      Capability(
          name="verifier",
          input_kind="item",
          serialize_input=False,
          build=stub(
              "verifier",
              lambda finding: {**finding, "issue": finding["issue"] + "!"},
          ),
      ),
      Capability(
          name="triager",
          input_kind="list",
          serialize_input=False,
          build=stub("triager", lambda findings: {"total": len(findings)}),
      ),
      Capability(
          name="formatter",
          input_kind="item",
          serialize_input=False,
          build=stub(
              "formatter", lambda r: {"note": f"audited {r['total']} files"}
          ),
      ),
  ])


@pytest.mark.asyncio
async def test_reuse_path_no_llm(monkeypatch):
  """Pre-seed a frozen spec + stub the registry: the demo must REUSE the plan
  (no planner/Gemini call) and still surface hash, capabilities, and output."""
  monkeypatch.setattr(demo, "_registry", _stub_registry)
  spec = _demo_spec()

  ss = InMemorySessionService()
  session = await ss.create_session(
      app_name="demo",
      user_id="u",
      state={
          "authored_workflow:frozen_spec": spec.model_dump(),
          "authored_workflow:frozen_spec_hash": "deadbeef0000",
      },
  )
  runner = Runner(app_name="demo", node=demo.root_agent, session_service=ss)

  out, reused_msg, authored_msg = None, False, False
  async for ev in runner.run_async(
      user_id="u",
      session_id=session.id,
      new_message=types.Content(parts=[types.Part(text="go")], role="user"),
  ):
    if isinstance(ev, Event) and ev.content and ev.content.parts:
      for p in ev.content.parts:
        if p.text and "Reusing frozen plan" in p.text:
          reused_msg = True
        if p.text and "Authored plan" in p.text:
          authored_msg = True
    if (
        isinstance(ev, Event)
        and isinstance(ev.output, dict)
        and "hash" in ev.output
    ):
      out = ev.output

  assert reused_msg and not authored_msg  # reused; planner NOT invoked
  assert out is not None
  assert out["reused"] is True
  assert (
      out["hash"] == "deadbeef0000"
  )  # same frozen hash, not re-derived from a new plan
  assert set(out["capabilities"]) == {
      "reviewer",
      "verifier",
      "triager",
      "formatter",
  }
  assert out["result"]["note"].startswith("audited")
  # cost visibility: 4 files x 2 pipeline stages + triager + formatter = 10.
  assert out["dispatches"] == 10
