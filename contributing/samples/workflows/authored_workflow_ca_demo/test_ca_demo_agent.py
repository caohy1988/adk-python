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

"""CI-safe tests for the BQ Conversational Analytics workflow demo (no LLM).

Each demo scenario's expected workflow shape is built by hand, validated +
lint-checked against the demo registry, and EXECUTED end-to-end on the real
ADK engine with the language capabilities (nl2sql, summaries, classifier,
skeptic) swapped for deterministic stubs — so all seven coordination shapes
the demo authors on camera are pinned in CI.
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
# Import as a PACKAGE (bq_ca_planner.agent), not a bare `agent` module — the
# sibling demo's tests import their own `agent`, and a bare import would
# collide in sys.modules when pytest collects both directories.
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "..", "authored_workflow_spike"))
from authoring import Binding  # noqa: E402
from authoring import Branch  # noqa: E402
from authoring import Capability  # noqa: E402
from authoring import CapabilityRegistry  # noqa: E402
from authoring import FanOut  # noqa: E402
from authoring import LoopUntil  # noqa: E402
from authoring import Pipeline  # noqa: E402
from authoring import PipelineStage  # noqa: E402
from authoring import Route  # noqa: E402
from authoring import SpecInterpreter  # noqa: E402
from authoring import StepRef  # noqa: E402
from authoring import WorkflowSpec  # noqa: E402
from authoring import WorkflowSpecValidator  # noqa: E402
from bq_ca_planner import agent as demo  # noqa: E402

_LLM_CAPS = (
    "nl2sql",
    "draft_or_repair_sql",
    "summarize_insight",
    "classify_question",
    "skeptic",
)


def _stub(name, fn):
  def build():
    @node(name=name)
    async def n(ctx, node_input):
      yield Event(output=fn(node_input))

    return n

  return build


def _stub_registry() -> CapabilityRegistry:
  """The demo registry with the live language capabilities stubbed."""
  real = demo._registry()
  stubs = [
      Capability(
          name="nl2sql",
          input_kind="item",
          serialize_input=False,
          build=_stub(
              "nl2sql",
              lambda s: {
                  "sql": (
                      "SELECT region, SUM(sale_price) AS revenue FROM"
                      " order_items GROUP BY region"
                  )
              },
          ),
      ),
      Capability(
          name="draft_or_repair_sql",
          input_kind="item",
          serialize_input=False,
          build=_stub(
              "draft_or_repair_sql",
              lambda s: {"sql": "SELECT status FROM orders LIMIT 10"},
          ),
      ),
      Capability(
          name="summarize_insight",
          input_kind="item",
          serialize_input=False,
          build=_stub(
              "summarize_insight",
              lambda s: {"insight": "US-West leads revenue."},
          ),
      ),
      Capability(
          name="classify_question",
          input_kind="item",
          serialize_input=False,
          build=_stub(
              "classify_question",
              lambda s: {
                  "category": (
                      "schema" if "mean" in json.dumps(s).lower() else "data"
                  )
              },
          ),
      ),
      Capability(
          name="skeptic",
          input_kind="item",
          serialize_input=False,
          build=_stub(
              "skeptic",
              lambda v: {"insight": str(v), "refuted": "1,000,000" in str(v)},
          ),
      ),
  ]
  passthrough = [
      cap for name, cap in real._by_name.items() if name not in _LLM_CAPS
  ]
  return CapabilityRegistry(stubs + passthrough)


# ----------------------------------------------------- expected shapes
def _expected_spec(key: str) -> WorkflowSpec:
  """The shape each scenario's planner recipe asks for, built by hand."""
  if key == "sequence":
    return WorkflowSpec(
        goal="revenue by region",
        steps=[
            StepRef(
                kind="step",
                id="sql",
                capability="nl2sql",
                input=Binding(source="task"),
            ),
            StepRef(
                kind="step",
                id="check",
                capability="dry_run",
                input=Binding(source="step", step="sql"),
            ),
            StepRef(
                kind="step",
                id="rows",
                capability="run_query",
                input=Binding(source="step", step="check"),
            ),
            StepRef(
                kind="step",
                id="sum",
                capability="summarize_insight",
                input=Binding(source="step", step="rows"),
            ),
        ],
        output=Binding(source="step", step="sum"),
    )
  if key == "fanout":
    return WorkflowSpec(
        goal="profile data quality",
        steps=[
            FanOut(
                kind="fan_out",
                id="profiles",
                over=Binding(source="task", path="tables"),
                capability="profile_table",
            ),
            StepRef(
                kind="step",
                id="report",
                capability="quality_report",
                input=Binding(source="step", step="profiles"),
            ),
        ],
        output=Binding(source="step", step="report"),
    )
  if key == "pipeline":
    return WorkflowSpec(
        goal="dashboard",
        steps=[
            Pipeline(
                kind="pipeline",
                id="panels",
                over=Binding(source="task", path="questions"),
                stages=[
                    PipelineStage(capability="nl2sql"),
                    PipelineStage(capability="dry_run"),
                ],
            ),
            StepRef(
                kind="step",
                id="sum",
                capability="summarize_insight",
                input=Binding(source="step", step="panels"),
            ),
        ],
        output=Binding(source="step", step="sum"),
    )
  if key == "branch":
    return WorkflowSpec(
        goal="route the question",
        steps=[
            StepRef(
                kind="step",
                id="cls",
                capability="classify_question",
                input=Binding(source="task"),
            ),
            Branch(
                kind="branch",
                id="route",
                on=Binding(source="step", step="cls", path="category"),
                routes=[
                    Route(
                        value="data",
                        block=[
                            StepRef(
                                kind="step",
                                id="d_sql",
                                capability="nl2sql",
                                input=Binding(source="task"),
                            ),
                            StepRef(
                                kind="step",
                                id="d_check",
                                capability="dry_run",
                                input=Binding(source="step", step="d_sql"),
                            ),
                            StepRef(
                                kind="step",
                                id="d_rows",
                                capability="run_query",
                                input=Binding(source="step", step="d_check"),
                            ),
                            StepRef(
                                kind="step",
                                id="d_sum",
                                capability="summarize_insight",
                                input=Binding(source="step", step="d_rows"),
                            ),
                        ],
                    ),
                    Route(
                        value="schema",
                        block=[
                            StepRef(
                                kind="step",
                                id="s_desc",
                                capability="describe_schema",
                                input=Binding(source="task"),
                            )
                        ],
                    ),
                ],
            ),
        ],
        output=Binding(source="step", step="route"),
    )
  if key == "loop":
    return WorkflowSpec(
        goal="sql self-repair",
        steps=[
            LoopUntil(
                kind="loop_until",
                id="repair",
                init=Binding(source="task"),
                body=[
                    StepRef(
                        kind="step",
                        id="draft",
                        capability="draft_or_repair_sql",
                        input=Binding(source="step", step="repair"),
                    ),
                    StepRef(
                        kind="step",
                        id="check",
                        capability="flaky_dry_run",
                        input=Binding(source="step", step="draft"),
                    ),
                ],
                until_capability="sql_ok",
                until_input=Binding(source="step", step="check"),
                max_iters=3,
            ),
        ],
        output=Binding(source="step", step="repair"),
    )
  if key == "adversarial":
    return WorkflowSpec(
        goal="audit insights",
        steps=[
            FanOut(
                kind="fan_out",
                id="verdicts",
                over=Binding(source="task", path="insights"),
                capability="skeptic",
            ),
            StepRef(
                kind="step",
                id="kept",
                capability="keep_verified",
                input=Binding(source="step", step="verdicts"),
            ),
        ],
        output=Binding(source="step", step="kept"),
    )
  if key == "tournament":
    return WorkflowSpec(
        goal="best chart",
        steps=[
            LoopUntil(
                kind="loop_until",
                id="bracket",
                init=Binding(source="task", path="chart_options"),
                body=[
                    StepRef(
                        kind="step",
                        id="pairs",
                        capability="pair_charts",
                        input=Binding(source="step", step="bracket"),
                    ),
                    FanOut(
                        kind="fan_out",
                        id="winners",
                        over=Binding(source="step", step="pairs"),
                        capability="judge_chart",
                    ),
                ],
                until_capability="single_chart",
                until_input=Binding(source="step", step="winners"),
                max_iters=3,
            ),
        ],
        output=Binding(source="step", step="bracket"),
    )
  raise KeyError(key)


async def _run(spec, registry, task_input):
  holder = {}

  @node(rerun_on_resume=True)
  async def parent(ctx, node_input):
    holder["out"] = await SpecInterpreter(registry, ctx).execute(
        spec, task_input
    )
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


# ----------------------------------------------------- tests
def test_stubs_tolerate_authored_binding_shapes():
  # The plan is MODEL-authored: a binding may hand a stub the whole step
  # output (dict), a dotted path into it (raw string), or a JSON-encoded
  # payload. The live error this pins: nl2sql -> dry_run with path='sql'
  # passed a raw SQL string and the stub assumed a dict.
  raw_sql = "SELECT region FROM order_items"
  assert demo._sql_of({"sql": raw_sql}) == raw_sql
  assert demo._sql_of(json.dumps({"sql": raw_sql})) == raw_sql
  assert demo._sql_of(raw_sql) == raw_sql
  assert demo._field_of({"valid": True}, "valid") is True
  assert demo._field_of(json.dumps({"valid": True}), "valid") is True
  assert demo._verdict_of(json.dumps({"insight": "x", "refuted": True})) == {
      "insight": "x",
      "refuted": True,
  }
  assert demo._verdict_of("just text")["refuted"] is False
  demo._FLAKY_CALLS["n"] = 1  # next call is even -> passes
  out = demo._flaky_dry_run(raw_sql)  # raw string input must not crash
  assert out["valid"] is True and out["sql"] == raw_sql


def test_rows_track_the_sql_window():
  # The mock executor returns a DIFFERENT canned set for a year-scale
  # window, so the demo output visibly tracks the question.
  q_sql = "SELECT ... WHERE created_at >= INTERVAL 1 QUARTER"
  y_sql = "SELECT ... WHERE created_at >= INTERVAL 1 YEAR"
  assert demo._rows_for(q_sql) == demo._CANNED_ROWS
  assert demo._rows_for(y_sql) == demo._CANNED_ROWS_YEAR
  assert demo._rows_for({"sql": y_sql}) == demo._CANNED_ROWS_YEAR


def test_text_of_extracts_user_message():
  assert demo._text_of("plain text") == "plain text"
  content = types.Content(
      role="user", parts=[types.Part(text="last year please")]
  )
  assert demo._text_of(content) == "last year please"

  class Wrapped:
    pass

  w = Wrapped()
  w.content = content
  assert demo._text_of(w) == "last year please"


def test_sequence_takes_live_question_others_stay_canned():
  q = "What was revenue by region last year?"
  assert demo._task_for("sequence", q) == {"question": q}
  # empty/whitespace falls back to the canned question
  assert demo._task_for("sequence", "  ") == demo.SCENARIOS["sequence"]["task"]
  # mode-selector scenarios keep their canned inputs
  assert demo._task_for("fanout", q) == demo.SCENARIOS["fanout"]["task"]


def test_root_agent_importable_and_named():
  assert isinstance(demo.root_agent, Workflow)
  assert demo.root_agent.name == "bq_ca_planner"


def test_registry_clean_and_typed():
  reg = demo._registry()
  for name in _LLM_CAPS + ("dry_run", "run_query", "profile_table"):
    assert name in reg
  assert reg.open_map_warnings() == []  # enumerated fields only


def test_scenario_routing():
  assert demo._scenario_for("What was revenue by region?") == "sequence"
  assert demo._scenario_for("Profile data quality please") == "fanout"
  assert demo._scenario_for("Build a dashboard for these") == "pipeline"
  assert demo._scenario_for("what does status Complete mean?") == "branch"
  assert demo._scenario_for("the dry run is unreliable, retry") == "loop"
  assert demo._scenario_for("audit these insights") == "adversarial"
  assert demo._scenario_for("pick the best chart") == "tournament"
  assert demo._scenario_for("hello") == "sequence"  # default
  # overlapping triggers: specialized intent must beat the generic fallback
  # ("revenue by region" is a sequence trigger, but these aren't questions).
  assert (
      demo._scenario_for("Pick the best chart for revenue by region.")
      == "tournament"
  )
  assert (
      demo._scenario_for("give me the best chart for revenue by region")
      == "tournament"
  )
  assert (
      demo._scenario_for("Profile data quality for revenue by region")
      == "fanout"
  )


def test_all_seven_shapes_validate_and_lint_clean():
  reg = demo._registry()
  for key in demo.SCENARIOS:
    warnings = WorkflowSpecValidator(reg).validate(_expected_spec(key))
    lints = [w for w in warnings if w.startswith("plan-quality")]
    assert lints == [], f"{key}: {lints}"


@pytest.mark.asyncio
async def test_sequence_executes():
  out = await _run(
      _expected_spec("sequence"),
      _stub_registry(),
      demo.SCENARIOS["sequence"]["task"],
  )
  assert out == {"insight": "US-West leads revenue."}


@pytest.mark.asyncio
async def test_fanout_executes_no_llm_needed():
  # profiling + report are deterministic mocks even in the LIVE registry.
  out = await _run(
      _expected_spec("fanout"),
      demo._registry(),
      demo.SCENARIOS["fanout"]["task"],
  )
  assert out == {"tables": 4, "worst_table": "users", "max_null_pct": 7.9}


@pytest.mark.asyncio
async def test_pipeline_executes_per_question():
  out = await _run(
      _expected_spec("pipeline"),
      _stub_registry(),
      demo.SCENARIOS["pipeline"]["task"],
  )
  assert out == {"insight": "US-West leads revenue."}


@pytest.mark.asyncio
async def test_branch_routes_schema_question():
  out = await _run(
      _expected_spec("branch"),
      _stub_registry(),
      demo.SCENARIOS["branch"]["task"],  # "...what does ... mean?" -> schema
  )
  assert "Complete" in out["answer"]


@pytest.mark.asyncio
async def test_loop_repairs_sql_exactly_once():
  demo._FLAKY_CALLS["n"] = 0
  out = await _run(
      _expected_spec("loop"),
      _stub_registry(),
      demo.SCENARIOS["loop"]["task"],
  )
  assert out["valid"] is True
  # odd call fails, even call passes -> exactly one repair iteration.
  assert demo._FLAKY_CALLS["n"] == 2


@pytest.mark.asyncio
async def test_adversarial_rejects_implausible_insight():
  out = await _run(
      _expected_spec("adversarial"),
      _stub_registry(),
      demo.SCENARIOS["adversarial"]["task"],
  )
  assert len(out["verified"]) == 2
  assert any("1,000,000" in r for r in out["rejected"])


@pytest.mark.asyncio
async def test_tournament_picks_best_chart_no_llm_needed():
  # pairing + judging are deterministic mocks even in the LIVE registry.
  out = await _run(
      _expected_spec("tournament"),
      demo._registry(),
      demo.SCENARIOS["tournament"]["task"],
  )
  assert out == ["bar"]
