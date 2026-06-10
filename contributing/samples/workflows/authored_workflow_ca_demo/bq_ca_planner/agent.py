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
import math
import os
import re
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

# ------------------------------------------------- micro-warehouse engine
# The "intelligent mock executor": a deterministic synthetic fact table
# (24 months x 4 regions x 4 categories) plus lightweight SQL-INTENT parsing.
# Instead of pattern-matching to a canned answer, run_query AGGREGATES the
# facts according to the query's grouping (month/region/category), window
# (INTERVAL N YEAR/QUARTER/MONTH), filters (country/region literals), and
# measure alias (SUM(...) AS <name>). Honest scope: it executes the query's
# INTENT, not its SQL — a real BigQuery backend is the production step.
_REGION_WEIGHT = {"US-West": 1.00, "US-East": 0.95, "EMEA": 0.72, "APAC": 0.46}
_CATEGORY_WEIGHT = {
    "Outerwear": 0.34,
    "Jeans": 0.27,
    "Activewear": 0.22,
    "Accessories": 0.17,
}
_MONTHS = [f"{y}-{m:02d}" for y in (2024, 2025) for m in range(1, 13)]
_BASE_MONTHLY = 142000.0


def _seasonal(i: int) -> float:
  # mild growth + yearly seasonality — deterministic, no RNG.
  return 1.0 + 0.18 * math.sin(i * math.pi / 6) + 0.012 * i


_FACTS = [
    {
        "month": month,
        "region": region,
        "category": category,
        "revenue": round(_BASE_MONTHLY * rw * cw * _seasonal(i), 2),
    }
    for i, month in enumerate(_MONTHS)
    for region, rw in _REGION_WEIGHT.items()
    for category, cw in _CATEGORY_WEIGHT.items()
]


def _query_engine(sql_text: str) -> list[dict]:
  """Aggregate the synthetic facts according to the SQL's intent."""
  s = (sql_text or "").lower()
  # time window: last N months from the warehouse's end (default: a quarter)
  m_y = re.search(r"interval\s+(\d+)\s+year", s)
  m_q = re.search(r"interval\s+(\d+)\s+quarter", s)
  m_m = re.search(r"interval\s+(\d+)\s+month", s)
  if m_y:
    n = int(m_y.group(1)) * 12
  elif m_q:
    n = int(m_q.group(1)) * 3
  elif m_m:
    n = int(m_m.group(1))
  elif "year" in s:
    n = 12
  else:
    n = 3
  months = set(_MONTHS[-min(n, len(_MONTHS)) :])
  facts = [f for f in _FACTS if f["month"] in months]
  # filters: country / region literals
  if "united states" in s or "'us'" in s:
    facts = [f for f in facts if f["region"].startswith("US-")]
  for region in _REGION_WEIGHT:
    if f"'{region.lower()}'" in s:
      facts = [f for f in facts if f["region"] == region]
  for category in _CATEGORY_WEIGHT:
    if f"'{category.lower()}'" in s:
      facts = [f for f in facts if f["category"] == category]
  # measure name: honor the SQL's alias when present
  alias = re.search(r"sum\([^)]*\)\s+as\s+([a-z_][a-z0-9_]*)", s)
  measure = alias.group(1) if alias else "revenue"
  # time grain: DATE_TRUNC(..., G) / EXTRACT(G FROM ...) / AS g / GROUP BY g.
  # Scope the GROUP BY check to the actual clause (stop at ORDER BY/LIMIT)
  # with INTERVAL phrases stripped — a trailing "INTERVAL 1 YEAR" window
  # must not read as a yearly grouping.
  gb_match = re.search(r"group by\s+(.*?)(?:\border by\b|\blimit\b|$)", s)
  gb_clause = re.sub(
      r"interval\s+\d+\s+\w+", "", gb_match.group(1) if gb_match else ""
  )
  grain = None
  for g in ("month", "week", "quarter", "year"):
    if (
        re.search(rf"date_trunc\([^)]*,\s*{g}\s*\)", s)
        or re.search(rf"extract\(\s*{g}\s+from", s)
        or re.search(rf"\bas\s+{g}\b", s)
        or re.search(rf"\b{g}\b", gb_clause)
    ):
      grain = "month" if g == "week" else g  # weekly facts -> monthly grain
      break
  if grain:

    def bucket(month: str) -> str:
      y, mm = month.split("-")
      if grain == "month":
        return month
      if grain == "quarter":
        return f"{y}-Q{(int(mm) - 1) // 3 + 1}"
      return y  # year

    agg: dict = {}
    for f in facts:
      b = bucket(f["month"])
      agg[b] = agg.get(b, 0.0) + f["revenue"]
    return [{grain: k, measure: round(v, 2)} for k, v in sorted(agg.items())]
  # categorical dimension
  if "category" in s or "department" in s:
    dim = "category"
  elif "region" in s or "country" in s:
    dim = "region"
  else:
    dim = None
  if dim is None:
    return [{measure: round(sum(f["revenue"] for f in facts), 2)}]
  agg = {}
  for f in facts:
    agg[f[dim]] = agg.get(f[dim], 0.0) + f["revenue"]
  return [
      {dim: k, measure: round(v, 2)}
      for k, v in sorted(agg.items(), key=lambda kv: -kv[1])
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


def _obj_of(v):
  """Accept a dict, a JSON-encoded dict/list string, or return None."""
  if isinstance(v, (dict, list)):
    return v
  if isinstance(v, str):
    try:
      parsed = json.loads(v)
      if isinstance(parsed, (dict, list)):
        return parsed
    except (ValueError, TypeError):
      pass
  return None


def _sql_of(v) -> str:
  """The SQL text from an Sql dict, a JSON string, or a raw SQL string."""
  obj = _obj_of(v)
  if isinstance(obj, dict):
    return str(obj.get("sql", ""))
  return v if isinstance(v, str) else ""


def _field_of(v, key, default=None):
  obj = _obj_of(v)
  if isinstance(obj, dict):
    return obj.get(key, default)
  return default


def _verdict_of(v) -> dict:
  obj = _obj_of(v)
  if isinstance(obj, dict) and "insight" in obj:
    return {
        "insight": str(obj["insight"]),
        "refuted": bool(obj.get("refuted")),
    }
  return {"insight": str(v), "refuted": False}


_VEGA_MARK = {"bar": "bar", "line": "line", "scatter": "point", "pie": "arc"}


def _ascii_bars(rows, width: int = 24) -> str:
  """A Unicode bar preview of (label, value) rows — renders in the chat."""
  pts = []
  for r in rows or []:
    if not isinstance(r, dict):
      continue
    label = next((str(v) for v in r.values() if isinstance(v, str)), "?")
    num = next(
        (float(v) for v in r.values() if isinstance(v, (int, float))), 0.0
    )
    pts.append((label, num))
  if not pts:
    return "(no rows)"
  mx = max(n for _, n in pts) or 1.0
  lw = max(len(label) for label, _ in pts)
  return "\n".join(
      f"{label:<{lw}}  {'█' * max(1, round(n / mx * width)):<{width}} "
      f" {n:>14,.2f}"
      for label, n in pts
  )


def _render_chart(v) -> dict:
  """Build a chart from whatever the authored binding hands over: query
  output (dict with rows), raw rows (list of dicts), a tournament winner
  (list with one chart-type string), or a bare chart-type string. Emits the
  Conversational-Analytics-style artifact: a Vega-Lite spec + a text
  preview the chat can render."""
  chart_type, rows, explicit = "bar", _CANNED_ROWS, False
  obj = _obj_of(v)
  if isinstance(obj, dict):
    rows = obj.get("rows", rows)
    if str(obj.get("chart_type", "")) in _VEGA_MARK:
      chart_type, explicit = str(obj["chart_type"]), True
  elif isinstance(obj, list) and obj:
    if isinstance(obj[0], dict):
      rows = obj
    elif str(obj[0]) in _VEGA_MARK:
      chart_type, explicit = str(obj[0]), True
  elif isinstance(v, str) and v in _VEGA_MARK:
    chart_type, explicit = v, True

  # date-shaped x labels (a time series) default to a LINE mark unless the
  # chart type was chosen explicitly (e.g. by the tournament winner).
  def _datelike(r) -> bool:
    return isinstance(r, dict) and any(
        isinstance(val, str) and re.match(r"^\d{4}(-\d{2}|-q\d|$)", val.lower())
        for val in r.values()
    )

  if not explicit and len(rows or []) >= 2 and all(map(_datelike, rows)):
    chart_type = "line"
  first = rows[0] if rows and isinstance(rows[0], dict) else {}
  x_field = next((k for k, v in first.items() if isinstance(v, str)), "label")
  y_field = next(
      (k for k, v in first.items() if isinstance(v, (int, float))), "value"
  )
  return {
      "chart_type": chart_type,
      "x_field": x_field,
      "y_field": y_field,
      "ascii": _ascii_bars(rows),
      "vega_lite": {
          "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
          "mark": _VEGA_MARK[chart_type],
          "data": {"values": rows},
          "encoding": {
              "x": {"field": x_field, "type": "nominal"},
              "y": {"field": y_field, "type": "quantitative"},
          },
      },
  }


def _chart_png(chart: dict):
  """Render the chart artifact to PNG bytes via matplotlib, or None.

  Optional dependency: without matplotlib the demo falls back to the text
  preview + Vega-Lite spec (which any Vega editor renders faithfully)."""
  try:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
  except ImportError:
    return None
  import io

  rows = chart["vega_lite"]["data"]["values"]
  labels = [
      next((str(v) for v in r.values() if isinstance(v, str)), "?")
      for r in rows
  ]
  values = [
      next((float(v) for v in r.values() if isinstance(v, (int, float))), 0.0)
      for r in rows
  ]
  kind = chart["chart_type"]
  fig, ax = plt.subplots(figsize=(6.4, 3.4), dpi=144)
  if kind == "pie":
    ax.pie(values, labels=labels, autopct="%1.0f%%")
  elif kind == "line":
    ax.plot(labels, values, marker="o", color="#4285F4")
  elif kind == "scatter":
    ax.scatter(labels, values, s=80, color="#4285F4")
  else:
    ax.bar(labels, values, color="#4285F4")
  if kind != "pie":
    ax.set_ylabel(chart.get("y_field", "value"))
    ax.grid(axis="y", alpha=0.3)
    ax.spines[["top", "right"]].set_visible(False)
  ax.set_title(
      f"{chart.get('y_field', 'value')} by {chart.get('x_field', 'label')}"
      f" ({kind})"
  )
  fig.tight_layout()
  buf = io.BytesIO()
  fig.savefig(buf, format="png")
  plt.close(fig)
  return buf.getvalue()


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
                  "sql": _sql_of(s),
                  "valid": "select" in _sql_of(s).lower(),
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
          build=_stub(
              "sql_ok",
              lambda s: bool(
                  _field_of(s, "valid", s if s is not None else False)
              ),
          ),
      ),
      Capability(
          name="run_query",
          input_kind="item",
          serialize_input=False,
          build=_stub(
              "run_query", lambda s: {"rows": _query_engine(_sql_of(s))}
          ),
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
          name="render_chart",
          input_kind="item",
          serialize_input=False,
          build=_stub("render_chart", _render_chart),
      ),
      Capability(
          name="keep_verified",
          input_kind="list",
          serialize_input=False,
          build=_stub(
              "keep_verified",
              lambda vs: {
                  "verified": [
                      v["insight"]
                      for v in map(_verdict_of, vs or [])
                      if not v["refuted"]
                  ],
                  "rejected": [
                      v["insight"]
                      for v in map(_verdict_of, vs or [])
                      if v["refuted"]
                  ],
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
        "question": str(_field_of(s, "question", "") or ""),
        "sql": _sql_of(s),
        "valid": False,
        "error": "Table not found: `thelook.order` (did you mean orders?)",
    }
  return {
      "question": str(_field_of(s, "question", "") or ""),
      "sql": _sql_of(s),
      "valid": True,
      "error": None,
  }


# ------------------------------------------------- scenarios
_CAPS_BLURB = (
    # NOTE: instruction strings must stay BRACE-FREE — ADK templates
    # "<curly>identifier<curly>" in instructions as session-state injection
    # and raises KeyError on unknown variables.
    "nl2sql (item: a question object -> Sql with field sql),"
    " draft_or_repair_sql (item: a question plus optional prior sql and error"
    " -> Sql), summarize_insight (item: rows or stats JSON -> Insight with"
    " field insight), classify_question (item: a question -> Category with"
    " field category equal to 'data' or 'schema'), skeptic (item: one insight"
    " -> Verdict with fields insight and refuted), dry_run (item: Sql -> object"
    " with sql, valid, error), flaky_dry_run (same as dry_run but may fail"
    " transiently), sql_ok (item: dry-run output -> bool), run_query (item:"
    " validated sql -> object with rows), profile_table (item: a table name ->"
    " stats object), quality_report (LIST of stats -> report object),"
    " describe_schema (item: a question -> object with answer), keep_verified"
    " (LIST of Verdicts -> object with verified and rejected), render_chart"
    " (item: query output with rows, or a chart-type winner -> a chart artifact"
    " with chart_type, ascii preview, and a vega_lite spec), pair_charts (LIST"
    " -> list of pairs), judge_chart (item: a pair -> the winner), single_chart"
    " (LIST -> bool)."
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
          shape="step → step → step → render_chart + step",
          triggers=("revenue by region", "sequence"),
          task={"question": q_region},
          recipe=(
              "Author, in order: (1) a step running nl2sql on the task;"
              " (2) a step running dry_run on it; (3) a step running"
              " run_query on that; (4) a step running render_chart on the"
              " run_query step's output; (5) a step running"
              " summarize_insight on the run_query step's output. Output ="
              " the summarize step."
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
          shape=(
              "loop_until(init=task.chart_options, body=[pair, fan_out])"
              " → render_chart"
          ),
          triggers=("best chart", "tournament"),
          task={"chart_options": ["pie", "bar", "line", "scatter"]},
          recipe=(
              "Author ONE loop_until: init = Binding(source='task',"
              " path='chart_options'); body = [(a) a step running"
              " pair_charts whose input is Binding(source='step', step=<the"
              " loop's own id>); (b) a fan_out over (a) running judge_chart"
              " per pair]; until_capability = single_chart with until_input"
              " = Binding(source='step', step=<the (b) fan_out>); max_iters"
              " = 3. Then (2) a step running render_chart on the loop's"
              " output (the winning chart type). Output = the render_chart"
              " step."
          ),
      ),
  }


SCENARIOS = _scenario_defs()


def _text_of(node_input) -> str:
  """The user's message text, whatever shape the node input arrives in."""
  if isinstance(node_input, str):
    return node_input
  for holder in (node_input, getattr(node_input, "content", None)):
    parts = getattr(holder, "parts", None)
    if parts:
      return " ".join(p.text for p in parts if getattr(p, "text", None))
  return str(node_input or "")


def _task_for(key: str, text: str) -> dict:
  """The scenario's task input. The ask-a-question scenario takes the LIVE
  user message as the question — so a re-send with a different question is
  TEMPLATE REUSE: the frozen plan unchanged, new task input flowing through
  it. Other scenarios keep their canned inputs (their prompts are mode
  selectors, not questions)."""
  task = dict(SCENARIOS[key]["task"])
  if key == "sequence" and text.strip():
    task = {"question": text.strip()}
  return task


def _scenario_for(text: str) -> str:
  """Specialized scenarios win over the generic ask-a-question fallback.

  'sequence' is the default for ANY question, so its triggers must never
  shadow a specialized intent — e.g. "best chart for revenue by region"
  contains both a tournament trigger and a sequence trigger and must route
  to the tournament.
  """
  t = (text or "").lower()
  for key, sc in SCENARIOS.items():
    if key == "sequence":
      continue  # fallback only — checked last by construction
    if any(trigger in t for trigger in sc["triggers"]):
      return key
  return "sequence"


def _planner_instruction(sc) -> str:
  keys = ", ".join(f"'{k}'" for k in sc["task"])
  return (
      "Author a WorkflowSpec using ONLY these capabilities: "
      + _CAPS_BLURB
      + " The task input JSON arrives as your input message; its keys:"
      f" {keys}. "
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
  text = _text_of(node_input)
  key = _scenario_for(text)
  sc = SCENARIOS[key]
  task = _task_for(key, text)
  state_key = f"authored_workflow:ca:{key}"

  task_note = f' — question: "{task["question"]}"' if key == "sequence" else ""
  yield _msg(
      f"🗂️ **Scenario: {sc['title']}** — expected shape `{sc['shape']}`,"
      " over mock `thelook_ecommerce`"
      f" ({', '.join(TABLES)}){task_note}."
  )

  # 1. LOAD-OR-AUTHOR (per-scenario frozen key: each shape replays
  # independently — re-send the same prompt to replay without the model).
  existing = ctx.state.get(state_key)
  if existing:
    spec = WorkflowSpec.model_validate(existing)
    spec_hash = _hash(spec)
    reused = True
    fresh_input = task != sc["task"]
    yield _msg(
        f"♻️ **Reusing frozen plan** for `{key}` — hash `{spec_hash}`. The"
        " model is NOT re-invoked; the exact prior plan is replayed"
        + (
            " — with your NEW question as the task input (**template"
            " reuse**: same plan, new data flowing through it)."
            if fresh_input
            else "."
        )
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
        planner, node_input=json.dumps(task), run_id=f"plan_{key}"
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
  result = await interp.execute(spec, task)
  elapsed = time.perf_counter() - t0
  for chart in (
      v
      for v in interp.state.values()
      if isinstance(v, dict) and "vega_lite" in v
  ):
    png = _chart_png(chart)
    if png is not None:
      yield Event(
          content=types.Content(
              role="model",
              parts=[
                  types.Part(
                      text=(
                          f"📈 **Chart ({chart['chart_type']})** — rendered"
                          " from the Conversational-Analytics-style"
                          " Vega-Lite artifact:"
                      )
                  ),
                  types.Part.from_bytes(data=png, mime_type="image/png"),
              ],
          )
      )
      yield _msg(
          "Vega-Lite spec (the portable artifact behind the image):\n"
          f"```json\n{json.dumps(chart['vega_lite'], indent=1)}\n```"
      )
    else:
      yield _msg(
          f"📈 **Chart ({chart['chart_type']})** — text preview + Vega-Lite"
          " spec (install matplotlib for an inline rendered image):\n```\n"
          f"{chart['ascii']}\n```\n```json\n"
          f"{json.dumps(chart['vega_lite'], indent=1)}\n```"
      )
  display = (
      {k: v for k, v in result.items() if k != "vega_lite"}
      if isinstance(result, dict)
      else result
  )
  yield _msg(
      f"📄 **Result:**\n```json\n{json.dumps(display, indent=1, default=str)}"
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
