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

"""ADK Web demo agent for RFC #93 — BigQuery Conversational Analytics planner.

One agent, SEVEN scenario prompts, each making the planner author a DIFFERENT
workflow shape over Conversational-Analytics-flavored capabilities (nl2sql,
dry_run, run_query, profiling, insight verification) against a mock
``thelook_ecommerce`` dataset:

  sequence      "What was revenue by region last quarter?"
  fan-out       "Profile data quality across the dataset tables."
  pipeline      "Build a dashboard for these three questions."
  branch        "Route my question: what does order status 'Complete' mean?"
  loop_until    "Answer with SQL self-repair — the dry run is unreliable."
  adversarial   "Audit these insights — verify each one independently."
  tournament    "Pick the best chart for revenue by region."

Each scenario runs the same machinery as the security-audit demo: author
(live planner) -> validate -> independence lints -> freeze (per-scenario
state key; re-send replays without re-invoking the model) -> execute on the
real engine via the #92 supervisor -> cost line. Query execution and dry-run
are deterministic mocks (no BigQuery project needed); language steps
(nl2sql, summaries, classification, skeptics) are live Gemini calls. Run:

    adk web contributing/samples/workflows/authored_workflow_ca_demo

Configure a model first (no hardcoded project):
    export GOOGLE_GENAI_USE_VERTEXAI=1 GOOGLE_CLOUD_PROJECT=<project>
    export GOOGLE_CLOUD_LOCATION=global SPIKE_GEMINI_MODEL=gemini-3.5-flash
"""

from __future__ import annotations

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
from authoring import Capability  # noqa: E402
from authoring import CapabilityRegistry  # noqa: E402
from authoring import independence_facts  # noqa: E402
from authoring import sha256_hex  # noqa: E402
from authoring import SpecInterpreter  # noqa: E402
from authoring import WorkflowSpec  # noqa: E402
from authoring import WorkflowSpecValidator  # noqa: E402

MODEL = os.environ.get("SPIKE_GEMINI_MODEL", "gemini-2.5-flash")
DET = types.GenerateContentConfig(temperature=0)

# ------------------------------------------------- mock thelook_ecommerce
# A miniature of bigquery-public-data.thelook_ecommerce — the dataset the
# Conversational Analytics docs demo against. run_query/dry_run/profiling
# are deterministic mocks so the demo needs no BigQuery project.
TABLES = {
    "orders": "order_id, user_id, status, created_at, num_of_item",
    "order_items": "id, order_id, product_id, sale_price, status",
    "products": "id, name, category, brand, retail_price, department",
    "users": "id, email, age, country, traffic_source, created_at",
}

_CANNED_ROWS = [
    {"region": "US-West", "revenue": 412300.50},
    {"region": "US-East", "revenue": 387910.25},
    {"region": "EMEA", "revenue": 295004.10},
    {"region": "APAC", "revenue": 188777.75},
]

_CANNED_PROFILES = {
    "orders": {"table": "orders", "row_count": 125210, "null_pct": 0.2},
    "order_items": {
        "table": "order_items",
        "row_count": 181830,
        "null_pct": 0.0,
    },
    "products": {"table": "products", "row_count": 29120, "null_pct": 3.4},
    "users": {"table": "users", "row_count": 100000, "null_pct": 7.9},
}

_SCHEMA_NOTES = {
    "default": (
        "orders.status takes Complete / Shipped / Processing / Cancelled /"
        " Returned; 'Complete' means the order was delivered and the return"
        " window has closed."
    )
}

# Simulated transient dry-run failure (repair-loop scenario): every ODD call
# fails, so EVERY run of the loop shows exactly one repair iteration —
# deterministic on camera and in CI, and replay behaves identically.
_FLAKY_CALLS = {"n": 0}

_JUDGE_RANK = {"bar": 0, "line": 1, "scatter": 2, "pie": 3}


# ------------------------------------------------- typed outputs (LLM caps)
class Sql(BaseModel):
  sql: str


class Insight(BaseModel):
  insight: str


class Category(BaseModel):
  category: Literal["data", "schema"]


class Verdict(BaseModel):
  insight: str
  refuted: bool


def _stub(name, fn):
  def build():
    @node(name=name)
    async def n(ctx, node_input):
      yield Event(output=fn(node_input))

    return n

  return build


def _llm(name, output_schema, instruction):
  return lambda: Agent(
      name=name,
      model=MODEL,
      output_schema=output_schema,
      generate_content_config=DET,
      instruction=instruction,
  )


def _registry() -> CapabilityRegistry:
  schema_blurb = "; ".join(f"{t}({c})" for t, c in TABLES.items())
  return CapabilityRegistry([
      # ---- live language capabilities (Gemini) ----
      Capability(
          name="nl2sql",
          input_kind="item",
          output_model=Sql,
          serialize_input=True,
          build=_llm(
              "nl2sql",
              Sql,
              "Translate the question in the input JSON to one BigQuery"
              f" StandardSQL SELECT over thelook_ecommerce: {schema_blurb}."
              " Output Sql.",
          ),
      ),
      Capability(
          name="draft_or_repair_sql",
          input_kind="item",
          output_model=Sql,
          serialize_input=True,
          build=_llm(
              "draft_or_repair_sql",
              Sql,
              "Input JSON has a question, and possibly a prior sql + error"
              " from a failed dry run. Draft (or repair, using the error)"
              " one BigQuery StandardSQL SELECT over thelook_ecommerce:"
              f" {schema_blurb}. Output Sql.",
          ),
      ),
      Capability(
          name="summarize_insight",
          input_kind="item",
          output_model=Insight,
          serialize_input=True,
          build=_llm(
              "summarize_insight",
              Insight,
              "Input: JSON query results (or profiling stats). Output"
              " Insight: one crisp analyst sentence.",
          ),
      ),
      Capability(
          name="classify_question",
          input_kind="item",
          output_model=Category,
          serialize_input=True,
          build=_llm(
              "classify_question",
              Category,
              "Classify the user question: 'data' if it needs a SQL query"
              " over the tables, 'schema' if it asks what a column/value"
              " means. Output Category.",
          ),
      ),
      Capability(
          name="skeptic",
          input_kind="item",
          output_model=Verdict,
          serialize_input=True,
          build=_llm(
              "skeptic",
              Verdict,
              "You are an adversarial data reviewer. Input: one insight"
              " about an e-commerce dataset (avg order ~ $60-90, 100k"
              " users). Try to REFUTE it; refuted=true if implausible."
              " Echo the insight. Output Verdict.",
          ),
      ),
      # ---- deterministic mocks (no BigQuery needed) ----
      Capability(
          name="dry_run",
          input_kind="item",
          serialize_input=False,
          build=_stub(
              "dry_run",
              lambda s: {
                  "sql": (s or {}).get("sql", ""),
                  "valid": "select" in str((s or {}).get("sql", "")).lower(),
                  "error": None,
              },
          ),
      ),
      Capability(
          name="flaky_dry_run",
          input_kind="item",
          serialize_input=False,
          build=_stub("flaky_dry_run", lambda s: _flaky_dry_run(s)),
      ),
      Capability(
          name="sql_ok",
          input_kind="item",
          serialize_input=False,
          build=_stub("sql_ok", lambda s: bool((s or {}).get("valid"))),
      ),
      Capability(
          name="run_query",
          input_kind="item",
          serialize_input=False,
          build=_stub("run_query", lambda s: {"rows": _CANNED_ROWS}),
      ),
      Capability(
          name="profile_table",
          input_kind="item",
          serialize_input=False,
          max_fan_out=20,
          build=_stub(
              "profile_table",
              lambda t: _CANNED_PROFILES.get(
                  str(t), {"table": str(t), "row_count": 0, "null_pct": 0.0}
              ),
          ),
      ),
      Capability(
          name="quality_report",
          input_kind="list",
          serialize_input=False,
          build=_stub(
              "quality_report",
              lambda profiles: {
                  "tables": len(profiles),
                  "worst_table": max(profiles, key=lambda p: p["null_pct"])[
                      "table"
                  ],
                  "max_null_pct": max(p["null_pct"] for p in profiles),
              },
          ),
      ),
      Capability(
          name="describe_schema",
          input_kind="item",
          serialize_input=False,
          build=_stub(
              "describe_schema",
              lambda q: {"answer": _SCHEMA_NOTES["default"]},
          ),
      ),
      Capability(
          name="keep_verified",
          input_kind="list",
          serialize_input=False,
          build=_stub(
              "keep_verified",
              lambda vs: {
                  "verified": [
                      v["insight"] for v in vs if not v.get("refuted")
                  ],
                  "rejected": [v["insight"] for v in vs if v.get("refuted")],
              },
          ),
      ),
      Capability(
          name="pair_charts",
          input_kind="list",
          serialize_input=False,
          build=_stub(
              "pair_charts",
              lambda lst: [lst[i : i + 2] for i in range(0, len(lst), 2)],
          ),
      ),
      Capability(
          name="judge_chart",
          input_kind="item",
          serialize_input=False,
          build=_stub(
              "judge_chart",
              lambda pair: min(pair, key=lambda c: _JUDGE_RANK.get(c, 99)),
          ),
      ),
      Capability(
          name="single_chart",
          input_kind="list",
          serialize_input=False,
          build=_stub("single_chart", lambda lst: len(lst) == 1),
      ),
  ])


def _flaky_dry_run(s):
  _FLAKY_CALLS["n"] += 1
  if _FLAKY_CALLS["n"] % 2 == 1:  # every odd call fails -> 1 repair per run
    return {
        "question": (s or {}).get("question", ""),
        "sql": (s or {}).get("sql", ""),
        "valid": False,
        "error": "Table not found: `thelook.order` (did you mean orders?)",
    }
  return {
      "question": (s or {}).get("question", ""),
      "sql": (s or {}).get("sql", ""),
      "valid": True,
      "error": None,
  }


# ------------------------------------------------- scenarios
_CAPS_BLURB = (
    "nl2sql (item: {question} -> Sql), draft_or_repair_sql (item: {question,"
    " sql?, error?} -> Sql), summarize_insight (item: rows/stats JSON ->"
    " Insight), classify_question (item: {question} -> Category with"
    " category 'data'|'schema'), skeptic (item: one insight -> Verdict),"
    " dry_run (item: Sql -> {sql, valid, error}), flaky_dry_run (same, may"
    " fail transiently), sql_ok (item: dry-run output -> bool), run_query"
    " (item: validated sql -> {rows}), profile_table (item: table name ->"
    " stats), quality_report (LIST of stats -> report), describe_schema"
    " (item: {question} -> {answer}), keep_verified (LIST of Verdicts ->"
    " {verified, rejected}), pair_charts (LIST -> list of pairs),"
    " judge_chart (item: pair -> winner), single_chart (LIST -> bool)."
)

_BINDING_RULES = (
    " Binding rules: Binding(source='task', path=<key>) reads the task"
    " input; Binding(source='step', step=<id>) chains steps; pipeline"
    " stages take the previous stage's per-item output automatically."
)


def _scenario_defs():
  """key -> (title, shape, triggers, task_input, planner recipe)."""
  q_region = "What was revenue by region last quarter?"
  return {
      "sequence": dict(
          title="Ask a question (sequence)",
          shape="step → step → step → step",
          triggers=("revenue by region", "sequence"),
          task={"question": q_region},
          recipe=(
              "Author, in order: (1) a step running nl2sql on the task;"
              " (2) a step running dry_run on it; (3) a step running"
              " run_query on that; (4) a step running summarize_insight on"
              " the rows. Output = the summarize step."
          ),
      ),
      "fanout": dict(
          title="Profile data quality (fan-out / synthesize)",
          shape="fan_out → step",
          triggers=("profile", "data quality"),
          task={"tables": list(TABLES)},
          recipe=(
              "Author: (1) a fan_out over task.tables running profile_table"
              " per table; (2) a step running quality_report on the fan_out"
              " output. Output = the report step."
          ),
      ),
      "pipeline": dict(
          title="Build a dashboard (pipeline)",
          shape="pipeline(nl2sql → dry_run) → step",
          triggers=("dashboard",),
          task={
              "questions": [
                  {"question": "Top 5 product categories by revenue?"},
                  {"question": "Monthly active users by traffic source?"},
                  {"question": "Return rate by department?"},
              ]
          },
          recipe=(
              "Author: (1) a pipeline over task.questions with two stages,"
              " nl2sql then dry_run, so each dashboard question is"
              " translated and validated per item; (2) a step running"
              " summarize_insight on the pipeline output. Output = the"
              " summarize step."
          ),
      ),
      "branch": dict(
          title="Route the question (classify & route)",
          shape="step → branch",
          triggers=("route", "what does", "mean"),
          task={"question": "What does order status 'Complete' mean?"},
          recipe=(
              "Author: (1) a step running classify_question on the task;"
              " (2) a branch on that step's 'category' field"
              " (Binding(source='step', step=<id>, path='category')) with"
              " TWO routes: value 'data' -> a block [nl2sql on task,"
              " dry_run, run_query, summarize_insight]; value 'schema' -> a"
              " block [describe_schema on task]. Output = the branch."
          ),
      ),
      "loop": dict(
          title="SQL self-repair (loop_until + loop-carried state)",
          shape="loop_until(init=task, body=[draft_or_repair, flaky_dry_run])",
          triggers=("repair", "unreliable", "retry"),
          task={"question": q_region},
          recipe=(
              "Author ONE loop_until: init = Binding(source='task'); body ="
              " [(a) a step running draft_or_repair_sql whose input is"
              " Binding(source='step', step=<the loop's own id>) — it reads"
              " the loop-carried value: the task on round 0, the failed"
              " dry-run output (sql + error) afterwards; (b) a step running"
              " flaky_dry_run on (a)]; until_capability = sql_ok with"
              " until_input = Binding(source='step', step=<the (b) step>);"
              " max_iters = 3. Output = the loop."
          ),
      ),
      "adversarial": dict(
          title="Audit insights (adversarial verification)",
          shape="fan_out(skeptic) → step(keep_verified)",
          triggers=("audit", "verify insights"),
          task={
              "insights": [
                  "Average order value is roughly $75.",
                  "The average order value is $1,000,000.",
                  "Most users arrive via organic search.",
              ]
          },
          recipe=(
              "Author: (1) a fan_out over task.insights running skeptic per"
              " insight; (2) a step running keep_verified on the fan_out"
              " output. Output = the keep_verified step."
          ),
      ),
      "tournament": dict(
          title="Pick the best chart (tournament)",
          shape="loop_until(init=task.chart_options, body=[pair, fan_out])",
          triggers=("best chart", "tournament"),
          task={"chart_options": ["pie", "bar", "line", "scatter"]},
          recipe=(
              "Author ONE loop_until: init = Binding(source='task',"
              " path='chart_options'); body = [(a) a step running"
              " pair_charts whose input is Binding(source='step', step=<the"
              " loop's own id>); (b) a fan_out over (a) running judge_chart"
              " per pair]; until_capability = single_chart with until_input"
              " = Binding(source='step', step=<the (b) fan_out>); max_iters"
              " = 3. Output = the loop."
          ),
      ),
  }


SCENARIOS = _scenario_defs()


def _scenario_for(text: str) -> str:
  t = (text or "").lower()
  for key, sc in SCENARIOS.items():
    if any(trigger in t for trigger in sc["triggers"]):
      return key
  return "sequence"


def _planner_instruction(sc) -> str:
  return (
      "Author a WorkflowSpec using ONLY these capabilities: "
      + _CAPS_BLURB
      + f" Task input: {json.dumps(sc['task'])}. "
      + sc["recipe"]
      + _BINDING_RULES
  )


def _msg(text: str) -> Event:
  return Event(
      content=types.Content(role="model", parts=[types.Part(text=text)])
  )


def _hash(spec: WorkflowSpec) -> str:
  return sha256_hex(spec.model_dump(mode="json"))[:12]


@node(rerun_on_resume=True)
async def plan_and_run(ctx: Context, node_input):
  reg = _registry()
  key = _scenario_for(str(node_input or ""))
  sc = SCENARIOS[key]
  state_key = f"authored_workflow:ca:{key}"

  yield _msg(
      f"🗂️ **Scenario: {sc['title']}** — expected shape `{sc['shape']}`,"
      " over mock `thelook_ecommerce`"
      f" ({', '.join(TABLES)})."
  )

  # 1. LOAD-OR-AUTHOR (per-scenario frozen key: each shape replays
  # independently — re-send the same prompt to replay without the model).
  existing = ctx.state.get(state_key)
  if existing:
    spec = WorkflowSpec.model_validate(existing)
    spec_hash = _hash(spec)
    reused = True
    yield _msg(
        f"♻️ **Reusing frozen plan** for `{key}` — hash `{spec_hash}`. The"
        " model is NOT re-invoked; the exact prior plan is replayed."
    )
  else:
    reused = False
    planner = Agent(
        name="planner",
        model=MODEL,
        output_schema=WorkflowSpec,
        generate_content_config=DET,
        instruction=_planner_instruction(sc),
    )
    raw = await ctx.run_node(
        planner, node_input=json.dumps(sc["task"]), run_id=f"plan_{key}"
    )
    spec = WorkflowSpec.model_validate(raw)
    spec_hash = _hash(spec)
    steps = " → ".join(s.kind for s in spec.steps)
    yield _msg(
        f"📋 **Authored plan** (`{steps}`):\n```json\n"
        f"{json.dumps(spec.model_dump(exclude_none=True), indent=1)}\n```"
    )

  # 2. VALIDATE + 2b. INDEPENDENCE LINTS.
  warnings = WorkflowSpecValidator(reg).validate(spec)
  lints = [w for w in warnings if w.startswith("plan-quality")]
  facts = "\n".join(f"   - {f}" for f in independence_facts(spec))
  yield _msg(
      f"✅ **Validation passed.** 🧪 plan-quality lints: {len(lints)}."
      f" Provenance (statically provable):\n{facts}"
      + (f"\n⚠️ {lints}" if lints else "")
  )

  # 3. FREEZE (per scenario).
  if not reused:
    ctx.state[state_key] = spec.model_dump()
    yield _msg(
        f"🔒 **Frozen** under `{state_key}` — hash `{spec_hash}`. Re-send"
        " this prompt: same plan, zero planner calls."
    )

  # 4. EXECUTE on the real engine via the #92 supervisor.
  t0 = time.perf_counter()
  interp = SpecInterpreter(reg, ctx)
  result = await interp.execute(spec, sc["task"])
  elapsed = time.perf_counter() - t0
  yield _msg(
      f"📄 **Result:**\n```json\n{json.dumps(result, indent=1, default=str)}"
      f"\n```\n📊 **Cost:** {interp.dispatch_count} capability dispatches in"
      f" {elapsed:.1f}s + "
      + ("0 planner calls (frozen replay)." if reused else "1 planner call.")
  )
  yield Event(
      output={
          "scenario": key,
          "hash": spec_hash,
          "reused": reused,
          "dispatches": interp.dispatch_count,
          "result": result,
      }
  )


root_agent = Workflow(
    name="bq_ca_planner",
    edges=[("START", plan_and_run)],
)
