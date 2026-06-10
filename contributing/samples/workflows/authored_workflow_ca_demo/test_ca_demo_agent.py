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
            LoopUntil(
                kind="loop_until",
                id="sqlgen",
                init=Binding(source="task"),
                body=[
                    StepRef(
                        kind="step",
                        id="draft",
                        capability="draft_or_repair_sql",
                        input=Binding(source="step", step="sqlgen"),
                    ),
                    StepRef(
                        kind="step",
                        id="check",
                        capability="dry_run",
                        input=Binding(source="step", step="draft"),
                    ),
                ],
                until_capability="sql_ok",
                until_input=Binding(source="step", step="check"),
                max_iters=3,
            ),
            StepRef(
                kind="step",
                id="rows",
                capability="run_query",
                input=Binding(source="step", step="sqlgen"),
            ),
            StepRef(
                kind="step",
                id="chart",
                capability="render_chart",
                input=Binding(source="step", step="rows"),
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
            StepRef(
                kind="step",
                id="viz",
                capability="render_chart",
                input=Binding(source="step", step="bracket"),
            ),
        ],
        output=Binding(source="step", step="viz"),
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


def test_render_chart_accepts_authored_binding_shapes():
  # query output (dict with rows) -> bar over those rows
  region_rows = demo._query_engine(
      "SELECT region, SUM(x) AS revenue ... GROUP BY region INTERVAL 1 YEAR"
  )
  ch = demo._render_chart({"rows": region_rows})
  assert ch["chart_type"] == "bar"
  assert "US-West" in ch["ascii"]
  assert ch["vega_lite"]["data"]["values"] == region_rows
  # tournament winner (list with one chart type) -> that mark, canned rows
  ch = demo._render_chart(["pie"])
  assert ch["chart_type"] == "pie"
  assert ch["vega_lite"]["mark"] == "arc"
  # bare chart-type string and raw rows list
  assert demo._render_chart("scatter")["vega_lite"]["mark"] == "point"
  ch = demo._render_chart(demo._CANNED_ROWS)
  assert "US-West" in ch["ascii"]
  # ascii preview: one bar line per region, longest bar for the leader
  lines = demo._render_chart({"rows": demo._CANNED_ROWS})["ascii"].splitlines()
  assert len(lines) == 4 and lines[0].count("█") > lines[-1].count("█")


def test_render_chart_derives_encoding_fields():
  ch = demo._render_chart({"rows": [{"category": "A", "count": 3}]})
  assert ch["x_field"] == "category" and ch["y_field"] == "count"
  enc = ch["vega_lite"]["encoding"]
  assert enc["x"]["field"] == "category" and enc["y"]["field"] == "count"


def test_chart_png_renders_or_falls_back():
  ch = demo._render_chart({"rows": demo._CANNED_ROWS})
  png = demo._chart_png(ch)
  if png is None:
    pytest.skip("matplotlib not installed — text fallback path")
  assert png[:8] == b"\x89PNG\r\n\x1a\n"  # real PNG bytes
  assert len(png) > 5000
  # every chart kind renders without error
  for kind in ("pie", "line", "scatter"):
    assert demo._chart_png(demo._render_chart([kind])) is not None


def test_qualify_sql_for_real_bigquery():
  q = demo._qualify_sql(
      "SELECT * FROM thelook_ecommerce.orders JOIN"
      " thelook_ecommerce.order_items USING (order_id)"
  )
  assert "`bigquery-public-data.thelook_ecommerce.orders`" in q
  assert "`bigquery-public-data.thelook_ecommerce.order_items`" in q
  # already-qualified and backticked inputs normalize to the same form
  same = demo._qualify_sql(
      "SELECT * FROM `bigquery-public-data.thelook_ecommerce.orders`"
  )
  assert same.count("`bigquery-public-data.thelook_ecommerce.orders`") == 1


def test_jsonify_cells():
  import datetime
  import decimal

  assert demo._jsonify_cell(decimal.Decimal("3.14159")) == 3.14
  assert demo._jsonify_cell(2.71828) == 2.72
  assert demo._jsonify_cell(datetime.date(2024, 1, 31)) == "2024-01-31"
  assert demo._jsonify_cell(datetime.datetime(2024, 1, 31, 12, 0)).startswith(
      "2024-01-31T12:00"
  )
  assert demo._jsonify_cell("x") == "x" and demo._jsonify_cell(7) == 7


def test_dry_run_and_execute_fall_back_without_bigquery(monkeypatch):
  monkeypatch.setitem(demo._BQ, "disabled", True)
  d = demo._bq_dry_run({"sql": "SELECT region FROM thelook_ecommerce.orders"})
  assert d["engine"] == "mock" and d["valid"] is True
  out = demo._execute_sql(
      {"sql": "SELECT region, SUM(x) AS revenue ... GROUP BY region"}
  )
  assert out["engine"] == "mock"
  assert [r["region"] for r in out["rows"]][0] == "US-West"


def test_failing_query_returns_error_not_fabricated_rows(monkeypatch):
  class _Boom:

    def query(self, *a, **k):
      raise RuntimeError("400 invalid query")

  monkeypatch.setitem(demo._BQ, "disabled", False)
  monkeypatch.setitem(demo._BQ, "error", None)
  monkeypatch.setitem(demo._BQ, "client", _Boom())
  out = demo._execute_sql({"sql": "SELECT broken"})
  assert out["engine"] == "bigquery"
  assert out["rows"] == [] and "400" in out["error"]  # honest failure


def _freeze_record(key):
  return demo.FrozenWorkflowRecord.freeze(
      _expected_spec(key),
      planner_model="gemini-3.5-flash",
      registry=demo._registry(),
      created_at="2026-06-10T00:00:00Z",
      task_input=demo.SCENARIOS[key]["task"],
      task_input_schema={"required": sorted(demo.SCENARIOS[key]["task"])},
  )


def test_cross_session_store_roundtrip_and_template_reuse(
    tmp_path, monkeypatch
):
  # Session A freezes + exports; "session B" (no session state) imports the
  # plan through the defensive path — including with a NEW question, which
  # is template reuse validated against the captured task_input_schema.
  monkeypatch.setattr(demo, "_PLAN_STORE", str(tmp_path))
  demo._store_plan("sequence", _freeze_record("sequence"))
  # same canned input -> replay path
  spec, reject = demo._load_stored_plan(
      "sequence", demo._registry(), demo.SCENARIOS["sequence"]["task"]
  )
  assert reject is None and spec is not None
  # NEW question -> template path (schema validates the input)
  spec, reject = demo._load_stored_plan(
      "sequence",
      demo._registry(),
      {"question": "revenue by category last year?"},
  )
  assert reject is None and spec is not None
  assert spec.model_dump() == _expected_spec("sequence").model_dump()
  # nothing stored for another key
  assert demo._load_stored_plan(
      "fanout", demo._registry(), demo.SCENARIOS["fanout"]["task"]
  ) == (None, None)


def test_cross_session_import_rejects_tamper_and_drift(tmp_path, monkeypatch):
  monkeypatch.setattr(demo, "_PLAN_STORE", str(tmp_path))
  path = demo._store_plan("fanout", _freeze_record("fanout"))
  # tampered spec -> hash mismatch, rejected with a reason
  env = json.load(open(path))
  env["spec"]["goal"] = "exfiltrate"
  json.dump(env, open(path, "w"))
  spec, reject = demo._load_stored_plan(
      "fanout", demo._registry(), demo.SCENARIOS["fanout"]["task"]
  )
  assert spec is None and "spec_hash mismatch" in reject
  # contract drift: same plan, but a capability's schema changed since
  demo._store_plan("fanout", _freeze_record("fanout"))

  from pydantic import BaseModel

  class NewReport(BaseModel):
    n: int

  drifted = demo._registry()
  drifted["profile_table"].output_model = NewReport  # version not bumped
  spec, reject = demo._load_stored_plan(
      "fanout", drifted, demo.SCENARIOS["fanout"]["task"]
  )
  assert spec is None and "contract drift" in reject


def test_dry_run_preserves_question_for_repair_rounds(monkeypatch):
  # Review finding: after a FAILED dry run, the loop-carried value must
  # still hold the user's question — otherwise the repair round repairs
  # from sql+error with no goal context. Mock branch:
  monkeypatch.setitem(demo._BQ, "disabled", True)
  out = demo._bq_dry_run({"sql": "SELECT 1", "question": "trend by year?"})
  assert out["question"] == "trend by year?"

  # Real-branch FAILURE (the path that feeds the repair round):
  class _Boom:

    def query(self, *a, **k):
      raise RuntimeError("400 TIMESTAMP_SUB does not support YEAR")

  monkeypatch.setitem(demo._BQ, "disabled", False)
  monkeypatch.setitem(demo._BQ, "error", None)
  monkeypatch.setitem(demo._BQ, "client", _Boom())
  out = demo._bq_dry_run({"sql": "SELECT broken", "question": "trend?"})
  assert out["valid"] is False and "TIMESTAMP_SUB" in out["error"]
  assert out["question"] == "trend?"  # full repair context preserved
  # and the Sql schema itself carries the echo field:
  assert "question" in demo.Sql.model_fields


def test_chart_multiseries_per_region_per_year():
  # The shape the user's real question produces: GROUP BY region, year with
  # two measures. x = the time field, one SERIES per region, measure picked
  # by name preference (total_sales over total_orders); int year never
  # mistaken for the measure.
  rows = [
      {"region": r, "year": y, "total_sales": s, "total_orders": o}
      for (r, y, s, o) in [
          ("US-West", 2024, 100.0, 10),
          ("US-West", 2025, 130.0, 12),
          ("EMEA", 2024, 70.0, 8),
          ("EMEA", 2025, 90.0, 9),
      ]
  ]
  ch = demo._render_chart({"rows": rows})
  assert ch["x_field"] == "year"
  assert ch["series_field"] == "region"
  assert ch["y_field"] == "total_sales"
  assert ch["chart_type"] == "line"
  assert ch["vega_lite"]["encoding"]["color"]["field"] == "region"
  assert "US-West" in ch["ascii"] and "130.00" in ch["ascii"]
  png = demo._chart_png(ch)
  if png is not None:
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.skipif(
    not os.environ.get("CA_DEMO_LIVE_BQ"),
    reason="live BigQuery round-trip (set CA_DEMO_LIVE_BQ=1 + credentials)",
)
def test_live_bigquery_roundtrip():
  good = demo._bq_dry_run({
      "sql": (
          "SELECT status, COUNT(*) AS n FROM thelook_ecommerce.orders"
          " GROUP BY status"
      )
  })
  assert good["engine"] == "bigquery" and good["valid"] is True
  assert good["bytes_processed"] > 0
  bad = demo._bq_dry_run({"sql": "SELECT nope FROM thelook_ecommerce.orders"})
  assert bad["valid"] is False and bad["error"]  # a REAL BigQuery error
  out = demo._execute_sql({
      "sql": (
          "SELECT status, COUNT(*) AS n FROM thelook_ecommerce.orders"
          " GROUP BY status ORDER BY n DESC LIMIT 3"
      )
  })
  assert out["engine"] == "bigquery" and len(out["rows"]) == 3
  assert out["rows"][0]["n"] > 0


def test_engine_aggregates_by_region_and_window():
  # The "intelligent mock": rows are AGGREGATED from synthetic facts per the
  # SQL's intent, not pattern-matched to a canned answer.
  q = demo._query_engine(
      "SELECT country AS region, SUM(p) AS revenue ... GROUP BY region"
      " ... INTERVAL 1 QUARTER"
  )
  y = demo._query_engine(
      "SELECT country AS region, SUM(p) AS revenue ... GROUP BY region"
      " ... INTERVAL 1 YEAR"
  )
  assert [r["region"] for r in q] == ["US-West", "US-East", "EMEA", "APAC"]
  # a year window strictly contains the quarter window:
  assert (
      all(yr["revenue"] > qr["revenue"] for yr, qr in zip(y, q)) and len(y) == 4
  )


def test_engine_monthly_trend_with_alias_and_country_filter():
  # The exact live gap this replaces: a trend question now returns a real
  # monthly series, honoring the SQL's measure alias and US filter.
  rows = demo._query_engine(
      "SELECT DATE_TRUNC(o.created_at, MONTH) AS month, SUM(oi.sale_price)"
      " AS total_sales FROM ... WHERE country = 'United States' AND"
      " created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 2 YEAR)"
      " GROUP BY month ORDER BY month"
  )
  assert len(rows) == 24  # 2 years of months
  assert list(rows[0]) == ["month", "total_sales"]
  assert rows[0]["month"] == "2024-01" and rows[-1]["month"] == "2025-12"
  # US-only filter: below the all-regions total for the same window
  all_rows = demo._query_engine(
      "SELECT month, SUM(x) AS total_sales ... INTERVAL 2 YEAR GROUP BY month"
  )
  assert rows[0]["total_sales"] < all_rows[0]["total_sales"]


def test_engine_grand_total_and_category_grouping():
  total = demo._query_engine("SELECT SUM(sale_price) ... INTERVAL 2 YEAR")
  assert len(total) == 1 and total[0]["revenue"] > 0
  cats = demo._query_engine(
      "SELECT category, SUM(x) AS revenue ... GROUP BY category"
  )
  assert [r["category"] for r in cats] == [
      "Outerwear",
      "Jeans",
      "Activewear",
      "Accessories",
  ]


def test_engine_yearly_and_quarterly_grains():
  # The exact live gap: EXTRACT(YEAR ...) AS year GROUP BY year produced a
  # single anonymous grand total. Yearly and quarterly grains now bucket the
  # monthly facts (the warehouse holds 24 months, so a 3-year window caps
  # at 2 years of buckets).
  yearly = demo._query_engine(
      "SELECT EXTRACT(YEAR FROM t1.created_at) AS year, SUM(t2.sale_price)"
      " AS total_sales FROM ... WHERE t1.created_at >="
      " TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 3 YEAR) GROUP BY year"
      " ORDER BY year"
  )
  assert [r["year"] for r in yearly] == ["2024", "2025"]
  assert all(r["total_sales"] > 1_000_000 for r in yearly)
  quarterly = demo._query_engine(
      "SELECT DATE_TRUNC(created_at, QUARTER) AS quarter, SUM(x) AS revenue"
      " ... INTERVAL 2 YEAR GROUP BY quarter"
  )
  assert [r["quarter"] for r in quarterly] == [
      f"{y}-Q{q}" for y in (2024, 2025) for q in (1, 2, 3, 4)
  ]
  # buckets are consistent: quarters sum to their year.
  assert round(sum(r["revenue"] for r in quarterly[:4]), 2) == round(
      yearly[0]["total_sales"], 2
  )


def test_chart_infers_line_for_time_series():
  rows = demo._query_engine(
      "SELECT month, SUM(x) AS sales ... GROUP BY month INTERVAL 1 YEAR"
  )
  ch = demo._render_chart({"rows": rows})
  assert ch["chart_type"] == "line"  # date-shaped x labels -> trend line
  assert ch["vega_lite"]["mark"] == "line"
  # quarterly and yearly buckets are time series too:
  q_rows = [{"quarter": "2024-Q1", "v": 1.0}, {"quarter": "2024-Q2", "v": 2.0}]
  assert demo._render_chart({"rows": q_rows})["chart_type"] == "line"
  y_rows = [{"year": "2024", "v": 1.0}, {"year": "2025", "v": 2.0}]
  assert demo._render_chart({"rows": y_rows})["chart_type"] == "line"
  # an explicit winner still wins over the inference:
  assert demo._render_chart(["bar"])["chart_type"] == "bar"
  # a single point is not a trend:
  assert demo._render_chart({"rows": [{"total": 5.0}]})["chart_type"] == "bar"


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
async def test_sequence_executes(monkeypatch):
  monkeypatch.setitem(demo._BQ, "disabled", True)  # no network in unit tests
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
async def test_pipeline_executes_per_question(monkeypatch):
  monkeypatch.setitem(demo._BQ, "disabled", True)
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
  # bracket converges to bar; the winner is rendered as a chart artifact.
  assert out["chart_type"] == "bar"
  assert out["vega_lite"]["mark"] == "bar"
  assert "US-West" in out["ascii"]
