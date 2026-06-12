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

import datetime
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
from authoring import export_plan  # noqa: E402
from authoring import FrozenWorkflowRecord  # noqa: E402
from authoring import import_plan  # noqa: E402
from authoring import independence_facts  # noqa: E402
from authoring import PlanImportError  # noqa: E402
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
    "order_items": "id, order_id, product_id, sale_price, status, created_at",
    "products": "id, name, category, brand, retail_price, department",
    "users": "id, email, age, country, traffic_source, created_at",
    "events": (
        "id, user_id, session_id, created_at, city, browser,"
        " traffic_source, uri, event_type"
    ),
    "inventory_items": (
        "id, product_id, created_at, sold_at, cost, product_category,"
        " product_brand, product_distribution_center_id"
    ),
    "distribution_centers": "id, name, latitude, longitude",
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


# ------------------------------------------------- REAL BigQuery backend
# When credentials allow, dry_run and run_query hit the REAL
# bigquery-public-data.thelook_ecommerce dataset (billed to
# GOOGLE_CLOUD_PROJECT) — real dry-run errors, real bytes-scanned, real
# multi-dimensional results. Safety rails: maximum_bytes_billed caps each
# query, results cap at _MAX_ROWS. Anything that fails falls back to the
# deterministic micro-warehouse above, so CI and credential-less machines
# keep working. CA_DEMO_USE_BIGQUERY=0 forces the mock.
_BQ_DATASET = "bigquery-public-data.thelook_ecommerce"
_MAX_BYTES_BILLED = 2 * 1024**3  # 2 GB per query
_MAX_ROWS = 500
_BQ = {
    "client": None,
    "disabled": os.environ.get("CA_DEMO_USE_BIGQUERY", "1") != "1",
    "error": None,
}


def _bq_client():
  if _BQ["disabled"] or _BQ["error"]:
    return None
  if _BQ["client"] is None:
    try:
      from google.cloud import bigquery  # optional dependency

      _BQ["client"] = bigquery.Client(
          project=os.environ.get("GOOGLE_CLOUD_PROJECT") or None
      )
    except Exception as e:  # no lib / no credentials -> mock warehouse
      _BQ["error"] = f"{type(e).__name__}: {e}"
      return None
  return _BQ["client"]


def _qualify_sql(sql: str) -> str:
  """Fully qualify bare thelook table refs for real BigQuery."""
  s = (sql or "").replace("`", "")
  s = re.sub(
      r"(?<![\w.-])thelook_ecommerce\.",
      "bigquery-public-data.thelook_ecommerce.",
      s,
  )
  return re.sub(
      r"bigquery-public-data\.thelook_ecommerce\.([A-Za-z_]\w*)",
      r"`bigquery-public-data.thelook_ecommerce.\1`",
      s,
  )


def _jsonify_cell(v):
  import datetime as _dt
  import decimal

  if isinstance(v, decimal.Decimal):
    return round(float(v), 2)
  if isinstance(v, float):
    return round(v, 2)
  if isinstance(v, (_dt.datetime, _dt.date)):
    return v.isoformat()
  return v


def _bq_dry_run(value) -> dict:
  sql = _qualify_sql(_sql_of(value))
  # Preserve the user's question through the dry run: after a FAILURE this
  # output becomes the loop-carried value, and the repair round needs full
  # context (question + sql + error), not just sql + error.
  question = str(_field_of(value, "question", "") or "")
  client = _bq_client()
  if client is None:
    return {
        "sql": sql,
        "question": question,
        "valid": "select" in sql.lower(),
        "error": None,
        "engine": "mock",
    }
  from google.cloud import bigquery

  try:
    job = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False),
    )
    return {
        "sql": sql,
        "question": question,
        "valid": True,
        "error": None,
        "engine": "bigquery",
        "bytes_processed": int(job.total_bytes_processed or 0),
    }
  except Exception as e:  # the REAL BigQuery error feeds the repair story
    return {
        "sql": sql,
        "question": question,
        "valid": False,
        "error": str(e)[:500],
        "engine": "bigquery",
    }


def _execute_sql(value) -> dict:
  sql = _qualify_sql(_sql_of(value))
  client = _bq_client()
  if client is not None:
    from google.cloud import bigquery

    try:
      job = client.query(
          sql,
          job_config=bigquery.QueryJobConfig(
              maximum_bytes_billed=_MAX_BYTES_BILLED
          ),
      )
      rows = [
          {k: _jsonify_cell(v) for k, v in dict(r).items()}
          for r in job.result(max_results=_MAX_ROWS)
      ]
      return {
          "rows": rows,
          "engine": "bigquery",
          "bytes_processed": int(job.total_bytes_processed or 0),
      }
    except Exception as e:
      # A failing query must NOT fabricate an answer from the mock — that
      # path is only for missing credentials. Return the failure honestly;
      # the repair loop upstream exists to prevent reaching here.
      return {"rows": [], "engine": "bigquery", "error": str(e)[:300]}
  return {"rows": _query_engine(sql), "engine": "mock"}


def query_thelook(sql: str) -> dict:
  """Run ONE read-only StandardSQL SELECT against the public dataset
  bigquery-public-data.thelook_ecommerce to check a claim. Use small
  aggregate queries (GROUP BY / COUNT / SUM); results are capped. Returns
  rows, the executing engine, and the real error when the SQL is invalid.
  """
  out = _execute_sql({"sql": sql})
  return {
      "rows": out.get("rows", [])[:50],
      "engine": out.get("engine"),
      "error": out.get("error"),
  }


# Mock fallback profiles (used WITHOUT credentials; clearly labeled via the
# engine field — with credentials, profiling queries the real __TABLES__).
_CANNED_PROFILES = {
    "orders": {"table": "orders", "row_count": 125000, "size_mb": 11.0},
    "order_items": {
        "table": "order_items",
        "row_count": 182000,
        "size_mb": 24.0,
    },
    "products": {"table": "products", "row_count": 29120, "size_mb": 4.8},
    "users": {"table": "users", "row_count": 100000, "size_mb": 27.0},
    "events": {"table": "events", "row_count": 2400000, "size_mb": 740.0},
    "inventory_items": {
        "table": "inventory_items",
        "row_count": 490000,
        "size_mb": 138.0,
    },
    "distribution_centers": {
        "table": "distribution_centers",
        "row_count": 10,
        "size_mb": 0.1,
    },
}


_TABLE_LIST_CACHE: dict = {}


def _live_table_list() -> list:
  """The dataset's ACTUAL non-empty tables from __TABLES__ (cached per
  process), falling back to the curated catalogue without credentials.
  Empty strays (e.g. the 0-row 'thelook_ecommerce-table' placeholder) are
  excluded — matching the production CA agent's 7-table scope."""
  if "tables" in _TABLE_LIST_CACHE:
    return _TABLE_LIST_CACHE["tables"]
  tables = list(TABLES)
  if _bq_client() is not None:
    out = _execute_sql({
        "sql": (
            "SELECT table_id FROM"
            " `bigquery-public-data.thelook_ecommerce.__TABLES__` WHERE"
            " row_count > 0 ORDER BY table_id"
        )
    })
    live = [r["table_id"] for r in out.get("rows") or []]
    if live:
      tables = live
  _TABLE_LIST_CACHE["tables"] = tables
  return tables


def _profile_table(value) -> dict:
  """REAL table profile from BigQuery __TABLES__ metadata (row count, size)
  when credentials allow; the canned fallback otherwise — engine-labeled."""
  name = str(value).strip().strip("`'\"")
  if _bq_client() is not None and re.fullmatch(r"[A-Za-z_][\w-]*", name):
    out = _execute_sql({
        "sql": (
            "SELECT table_id, row_count, size_bytes FROM"
            " `bigquery-public-data.thelook_ecommerce.__TABLES__` WHERE"
            f" table_id = '{name}'"
        )
    })
    rows = out.get("rows") or []
    if rows:
      return {
          "table": rows[0]["table_id"],
          "row_count": int(rows[0]["row_count"]),
          "size_mb": round(float(rows[0]["size_bytes"]) / 1048576, 1),
          "engine": "bigquery",
      }
  prof = dict(
      _CANNED_PROFILES.get(
          name, {"table": name, "row_count": 0, "size_mb": 0.0}
      )
  )
  prof["engine"] = "mock"
  return prof


_JUDGE_RANK = {"bar": 0, "line": 1, "scatter": 2, "pie": 3}


# ------------------------------------------------- typed outputs (LLM caps)
class Sql(BaseModel):
  sql: str
  # Echoed by the SQL-drafting capabilities so the loop-carried value still
  # holds the user's question after a FAILED dry run — the repair round
  # repairs with full context (question + sql + real error), not sql+error.
  question: str = ""


class DryRunResult(BaseModel):
  sql: str
  valid: bool
  error: str | None = None
  engine: str = "mock"
  question: str = ""
  bytes_processed: int = 0


class QueryResult(BaseModel):
  rows: list[dict]
  engine: str = "mock"
  bytes_processed: int = 0
  error: str | None = None


class ChartArtifact(BaseModel):
  chart_type: str
  x_field: str
  y_field: str
  series_field: str | None = None
  ascii: str
  vega_lite: dict


class TableProfile(BaseModel):
  table: str
  row_count: int
  size_mb: float
  engine: str = "mock"


class QualityReport(BaseModel):
  tables: int
  total_rows: int
  largest_table: str
  total_size_mb: float


class SchemaAnswer(BaseModel):
  answer: str


class VerifiedInsights(BaseModel):
  verified: list[str]
  rejected: list[str]


class Insight(BaseModel):
  insight: str


class Category(BaseModel):
  category: Literal["data", "schema"]


class Verdict(BaseModel):
  insight: str
  refuted: bool
  reason: str = ""  # the skeptic must SHOW ITS WORK — one-sentence judgment


class Intent(BaseModel):
  """The conversational gate's verdict for untriggered messages."""

  intent: Literal["data", "meta", "chat"]
  reply: str = ""


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
        "reason": str(obj.get("reason", "") or ""),
    }
  return {"insight": str(v), "refuted": False, "reason": ""}


def _verdict_lines(state: dict):
  """Render every skeptic verdict found in interpreter state — one line per
  insight, with the skeptic's stated reason — or [] when no audit ran."""
  lines = []
  for value in state.values():
    if not (isinstance(value, list) and value):
      continue
    verdicts = [_verdict_of(item) for item in value]
    if not all(
        _obj_of(item) and "refuted" in (_obj_of(item) or {}) for item in value
    ):
      continue
    for v in verdicts:
      mark = "❌ REFUTED" if v["refuted"] else "✅ upheld"
      reason = f" — {v['reason']}" if v["reason"] else ""
      lines.append(f"{mark} — \"{v['insight']}\"{reason}")
  return lines


_VEGA_MARK = {"bar": "bar", "line": "line", "scatter": "point", "pie": "arc"}


def _ascii_bars(rows, x=None, y=None, series=None, width: int = 24) -> str:
  """A Unicode bar preview of the rows — renders in the chat. Uses the
  derived x/y/series fields when given (so an integer `year` column is
  never mistaken for the measure); falls back to first-str/first-number."""
  pts = []
  for r in rows or []:
    if not isinstance(r, dict):
      continue
    if x in r:
      label = str(r.get(x, "?"))
    else:
      label = next((str(v) for v in r.values() if isinstance(v, str)), "?")
    if series and series in r:
      label = f"{r[series]} {label}"
    if y in r:
      num = float(r.get(y) or 0.0)
    else:
      num = next(
          (float(v) for v in r.values() if isinstance(v, (int, float))), 0.0
      )
    pts.append((label, num))
  if not pts:
    return "(no rows)"
  pts = pts[:40]  # keep the chat readable for wide results
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
  chart_type, rows, explicit = "bar", None, False
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
  if rows is None:
    # No rows handed over (e.g. the tournament passes only the winning
    # chart type): chart REAL revenue-by-region data, not canned values.
    rows = (
        _execute_sql({
            "sql": (
                "SELECT u.country AS region, SUM(oi.sale_price) AS revenue"
                " FROM thelook_ecommerce.order_items AS oi JOIN"
                " thelook_ecommerce.orders AS o ON oi.order_id = o.order_id"
                " JOIN thelook_ecommerce.users AS u ON o.user_id = u.id"
                " GROUP BY region ORDER BY revenue DESC LIMIT 8"
            )
        }).get("rows")
        or _CANNED_ROWS
    )
  first = rows[0] if rows and isinstance(rows[0], dict) else {}
  timeish = ("year", "quarter", "month", "week", "date", "day")
  str_fields = [k for k, v in first.items() if isinstance(v, str)]
  time_fields = [
      k
      for k, v in first.items()
      if k.lower() in timeish
      or (isinstance(v, str) and re.match(r"^\d{4}([-/]\d{2})?", v))
  ]
  num_fields = [
      k
      for k, v in first.items()
      if isinstance(v, (int, float))
      and not isinstance(v, bool)
      and k not in time_fields
  ]
  x_field = (
      time_fields[0]
      if time_fields
      else (str_fields[0] if str_fields else "label")
  )
  # a second categorical field becomes the SERIES (one line per value) —
  # e.g. GROUP BY region, year -> x=year, one series per region.
  series_field = next(
      (k for k in str_fields if k != x_field and k not in time_fields), None
  )
  if series_field is None and len(time_fields) == 0:
    series_field = None

  def _measure_rank(k: str) -> int:
    kl = k.lower()
    if "revenue" in kl or "sales" in kl:
      return 0
    if "total" in kl or "amount" in kl:
      return 1
    return 2

  y_field = sorted(num_fields, key=_measure_rank)[0] if num_fields else "value"
  if series_field and not explicit:
    chart_type = "line"  # comparing series over a dimension -> lines
  encoding = {
      "x": {"field": x_field, "type": "nominal"},
      "y": {"field": y_field, "type": "quantitative"},
  }
  if series_field:
    encoding["color"] = {"field": series_field, "type": "nominal"}
  return {
      "chart_type": chart_type,
      "x_field": x_field,
      "y_field": y_field,
      "series_field": series_field,
      "ascii": _ascii_bars(rows, x=x_field, y=y_field, series=series_field),
      "vega_lite": {
          "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
          "mark": _VEGA_MARK[chart_type],
          "data": {"values": rows},
          "encoding": encoding,
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
  x, y = chart.get("x_field", "label"), chart.get("y_field", "value")
  series = chart.get("series_field")
  kind = chart["chart_type"]
  fig, ax = plt.subplots(figsize=(6.8, 3.6), dpi=144)
  if series:
    # one line per series value (e.g. per region), x shared
    by_series: dict = {}
    for r in rows:
      by_series.setdefault(str(r.get(series, "?")), []).append(
          (str(r.get(x, "?")), float(r.get(y) or 0.0))
      )
    for name, pts in sorted(by_series.items()):
      pts.sort()
      ax.plot([p[0] for p in pts], [p[1] for p in pts], marker="o", label=name)
    ax.legend(fontsize=8)
  else:
    labels = [str(r.get(x, "?")) for r in rows]
    values = [float(r.get(y) or 0.0) for r in rows]
    if kind == "pie":
      ax.pie(values, labels=labels, autopct="%1.0f%%")
    elif kind == "line":
      ax.plot(labels, values, marker="o", color="#4285F4")
    elif kind == "scatter":
      ax.scatter(labels, values, s=80, color="#4285F4")
    else:
      ax.bar(labels, values, color="#4285F4")
  if kind != "pie" or series:
    ax.set_ylabel(y)
    ax.grid(axis="y", alpha=0.3)
    ax.spines[["top", "right"]].set_visible(False)
    if len({str(r.get(x, "")) for r in rows}) > 8:
      ax.tick_params(axis="x", labelrotation=60, labelsize=7)
  title = f"{y} by {x}" + (f" per {series}" if series else "") + f" ({kind})"
  ax.set_title(title)
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
              " StandardSQL SELECT over the public dataset"
              " bigquery-public-data.thelook_ecommerce (use fully-qualified"
              f" table names): {schema_blurb}. Output Sql, echoing the"
              " question field.",
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
              " from a failed dry run. If there is an error, REPAIR the sql"
              " using it; if the sql is valid (no error), return it"
              " unchanged. Otherwise draft"
              " one BigQuery StandardSQL SELECT over the public dataset"
              " bigquery-public-data.thelook_ecommerce (fully-qualified"
              f" table names): {schema_blurb}. Output Sql, echoing the"
              " question field.",
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
          # v2: the skeptic became DATA-GROUNDED (a real query tool) — a
          # semantic contract change, so stored plans drift-reject and
          # re-author rather than silently reusing the plausibility-only
          # skeptic. ADK supports output_schema + tools together: tools in
          # the thought loop, structure enforced on the final output.
          version="2",
          build=lambda: Agent(
              name="skeptic",
              model=MODEL,
              output_schema=Verdict,
              generate_content_config=DET,
              tools=[query_thelook],
              instruction=(
                  "You are an adversarial DATA reviewer with a real"
                  " BigQuery tool. Input: one insight/claim about the"
                  " public dataset bigquery-public-data.thelook_ecommerce"
                  f" ({schema_blurb}). Do NOT judge from priors: VERIFY the"
                  " claim by running 1-3 small aggregate SELECTs with the"
                  " query_thelook tool and compare the actual numbers to"
                  " the claim. Then output Verdict: echo the claim as"
                  " insight; refuted=true only if the data contradicts it;"
                  " reason = one sentence citing the numbers you queried"
                  " (note caveats like partial years)."
              ),
          ),
      ),
      # ---- deterministic mocks (no BigQuery needed) ----
      Capability(
          name="dry_run",
          input_kind="item",
          output_model=DryRunResult,
          serialize_input=False,
          build=_stub("dry_run", _bq_dry_run),
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
          output_model=QueryResult,
          serialize_input=False,
          build=_stub("run_query", _execute_sql),
      ),
      Capability(
          name="profile_table",
          output_model=TableProfile,
          input_kind="item",
          serialize_input=False,
          max_fan_out=20,
          build=_stub("profile_table", _profile_table),
      ),
      Capability(
          name="quality_report",
          output_model=QualityReport,
          input_kind="list",
          serialize_input=False,
          build=_stub(
              "quality_report",
              lambda profiles: {
                  "tables": len(profiles),
                  "total_rows": sum(
                      int(p.get("row_count", 0)) for p in profiles
                  ),
                  "largest_table": (
                      max(profiles, key=lambda p: p.get("row_count", 0))[
                          "table"
                      ]
                      if profiles
                      else ""
                  ),
                  "total_size_mb": round(
                      sum(float(p.get("size_mb", 0)) for p in profiles), 1
                  ),
              },
          ),
      ),
      Capability(
          name="describe_schema",
          output_model=SchemaAnswer,
          input_kind="item",
          serialize_input=True,
          # v2: answers metadata questions from the REAL dataset (it queries
          # DISTINCT values / counts) instead of a canned sentence.
          version="2",
          build=lambda: Agent(
              name="describe_schema",
              model=MODEL,
              output_schema=SchemaAnswer,
              generate_content_config=DET,
              tools=[query_thelook],
              instruction=(
                  "Answer metadata/meaning questions about the public"
                  " dataset bigquery-public-data.thelook_ecommerce"
                  f" ({schema_blurb}). QUERY the real data with the"
                  " query_thelook tool (e.g. SELECT DISTINCT values, small"
                  " counts) rather than answering from priors. Output"
                  " SchemaAnswer: a concise answer grounded in the queried"
                  " values."
              ),
          ),
      ),
      Capability(
          name="render_chart",
          input_kind="item",
          output_model=ChartArtifact,
          serialize_input=False,
          build=_stub("render_chart", _render_chart),
      ),
      Capability(
          name="keep_verified",
          output_model=VerifiedInsights,
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


# ------------------------------------------------- scenarios
_CAPS_BLURB = (
    # NOTE: instruction strings must stay BRACE-FREE — ADK templates
    # "<curly>identifier<curly>" in instructions as session-state injection
    # and raises KeyError on unknown variables.
    "nl2sql (item: a question object -> Sql with field sql),"
    " draft_or_repair_sql (item: a question plus optional prior sql and error"
    " -> Sql), summarize_insight (item: rows or stats JSON -> Insight with"
    " field insight), classify_question (item: a question -> Category with"
    " field category equal to 'data' or 'schema'), skeptic (item: one —"
    " data-grounded: it runs real verification queries via its query_thelook"
    " tool; insight -> Verdict with fields insight and refuted), dry_run (item:"
    " Sql or a task with sql -> object with sql, valid, error — the REAL"
    " BigQuery dry-run), sql_ok (item: dry-run output -> bool), run_query"
    " (item: validated sql -> object with rows), profile_table (item: a table"
    " name -> stats object), quality_report (LIST of stats -> report object),"
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
          title="Ask a question (draft → REAL dry-run → repair → execute)",
          shape=(
              "loop_until(draft_or_repair → real dry_run) → run_query →"
              " render_chart + step"
          ),
          triggers=("revenue by region", "sequence"),
          task={"question": q_region},
          recipe=(
              "Author, in order: (1) ONE loop_until for SQL drafting with"
              " self-repair: init = Binding(source='task'); body = [(a) a"
              " step running draft_or_repair_sql whose input is"
              " Binding(source='step', step=<the loop's own id>) — round 0"
              " reads the task, later rounds read the failed dry-run output"
              " (sql + error); (b) a step running dry_run on (a)];"
              " until_capability = sql_ok with until_input ="
              " Binding(source='step', step=<the (b) step>); max_iters = 3."
              " (2) a step running run_query on the loop's output. (3) a"
              " step running render_chart on the run_query step's output."
              " (4) a step running summarize_insight on the run_query"
              " step's output. Output = the summarize step."
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
          title="SQL self-repair from a REAL broken query (loop_until)",
          shape="loop_until(REAL dry_run → repair) → run_query",
          triggers=("repair", "unreliable", "retry", "broken"),
          task={
              "question": q_region,
              "sql": (  # 'order' instead of 'orders' -> a REAL not-found error
                  "SELECT u.country AS region, SUM(oi.sale_price) AS revenue"
                  " FROM thelook_ecommerce.order_items AS oi JOIN"
                  " thelook_ecommerce.order AS o ON oi.order_id ="
                  " o.order_id JOIN thelook_ecommerce.users AS u ON"
                  " o.user_id = u.id GROUP BY region ORDER BY revenue DESC"
              ),
          },
          recipe=(
              "Author, in order: (1) ONE loop_until: init ="
              " Binding(source='task'); body = [(a) a step running dry_run"
              " whose input is Binding(source='step', step=<the loop's own"
              " id>) — round 0 checks the task's sql, later rounds check"
              " the repaired sql; (b) a step running draft_or_repair_sql"
              " on (a) — it reads question + sql + the REAL BigQuery error"
              " and outputs a fixed Sql (if there is no error, return the"
              " sql unchanged)]; until_capability = sql_ok with until_input"
              " = Binding(source='step', step=<the (a) step>); max_iters ="
              " 3. (2) a step running run_query on the loop's output."
              " Output = the run_query step."
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


def _extract_insights(text: str):
  """Insights inlined in an audit ask ('audit this insight: X' / lists
  split on ';' or newlines), or None when the message is trigger-only."""
  t = (text or "").strip()
  tl = t.lower()
  for trig in ("verify insights", "audit", "verify"):
    i = tl.find(trig)
    if i < 0:
      continue
    rest = t[i + len(trig) :]
    rest = re.sub(
        r"^[\s:,\-—]*((these|this|the|my)\s+)?(insights?|claims?|ingisht\w*)?[\s:,\-—]*",
        "",
        rest,
        flags=re.I,
    )
    rest = rest.strip().strip('"').rstrip("?!.").strip()
    if len(rest) >= 12:
      parts = [s.strip() for s in re.split(r"[;\n]+", rest) if s.strip()]
      return parts or None
    return None
  return None


def _task_for(key: str, text: str, last_insight: str | None = None) -> dict:
  """The scenario's task input. LIVE inputs where they make sense:

  * sequence: the user's message IS the question;
  * adversarial: insights inlined in the message are audited; with none
    inlined, the session's LAST generated insight ('audit that'); only
    then the canned demo set.
  Other scenarios keep canned inputs (their prompts are mode selectors)."""
  task = dict(SCENARIOS[key]["task"])
  if key == "sequence" and text.strip():
    task = {"question": text.strip()}
  if key == "fanout":
    task = {"tables": _live_table_list()}  # whatever REALLY exists
  if key == "adversarial":
    inline = _extract_insights(text)
    if inline:
      task = {"insights": inline}
    elif last_insight:
      task = {"insights": [last_insight]}
  return task


def _matched_scenario(text: str):
  """The scenario whose trigger the message hits, or None (gate decides)."""
  t = (text or "").lower()
  for key, sc in SCENARIOS.items():
    if key == "sequence":
      continue
    if any(trigger in t for trigger in sc["triggers"]):
      return key
  return None


def _describe_workflows() -> str:
  """A brace-free catalogue of the workflow kinds, built from SCENARIOS so
  it never drifts from the actual demo."""
  lines = []
  for sc in SCENARIOS.values():
    shape = sc["shape"].replace("{", "(").replace("}", ")")
    lines.append(f"* {sc['title']} — shape: {shape}")
  return "\n".join(lines)


def _intent_agent() -> Agent:
  # The conversational gate: small questions should not pay orchestration
  # overhead (the RFC's no-plan escape hatch). NOTE: instruction must stay
  # brace-free (ADK templates curly identifiers as state injection).
  return Agent(
      name="intent_gate",
      model=MODEL,
      output_schema=Intent,
      generate_content_config=DET,
      instruction=(
          "You are the front door of a BigQuery Conversational Analytics"
          " demo agent. It answers questions over the public"
          " bigquery-public-data.thelook_ecommerce dataset (orders,"
          " order_items, products, users) by AUTHORING typed workflows:\n"
          + _describe_workflows()
          + "\nClassify the user's message. If it is a question answerable"
          " from the e-commerce data (metrics, trends, segments, SQL-able"
          " asks), output intent='data' with an empty reply. If it asks"
          " what you can do, which workflows you can issue, how to use"
          " you, or about your design, output intent='meta' and write a"
          " genuinely helpful reply: list the workflow kinds above, one"
          " example prompt each, and mention that plans are validated,"
          " frozen, replayable across sessions, and run on real BigQuery."
          " Otherwise output intent='chat' with a brief friendly reply"
          " that points at what you can do. Reply in plain markdown."
      ),
  )


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


# ------------------------------------------------- cross-session plan store
# Frozen plans outlive the session: on freeze, the FULL FrozenWorkflowRecord
# is exported as a portable envelope to disk (a stand-in for the
# ArtifactService in production — RFC Q1). A NEW session imports it through
# the RFC's DEFENSIVE import: spec_hash recomputed, re-validated against the
# CURRENT registry, manual-version + DECLARED-contract drift (input kind +
# declared output schema; capabilities without a declared output model rely
# on manual versions) fail loudly, and
# the new task input is validated against the captured task_input_schema
# (template reuse). Drift never silently replays a stale plan — it falls
# back to authoring fresh, with the rejection shown.
_PLAN_STORE = os.path.join(os.getcwd(), "ca_plan_store")


def _store_plan(key: str, record: FrozenWorkflowRecord) -> str:
  os.makedirs(_PLAN_STORE, exist_ok=True)
  path = os.path.join(_PLAN_STORE, f"{key}.json")
  with open(path, "w") as f:
    json.dump(export_plan(record), f, indent=1)
  return path


def _load_stored_plan(key: str, registry, task):
  """Returns (spec, None) on a valid import, (None, reason) on a rejected
  or unreadable envelope, (None, None) when nothing is stored."""
  path = os.path.join(_PLAN_STORE, f"{key}.json")
  if not os.path.exists(path):
    return None, None
  try:
    with open(path) as f:
      envelope = json.load(f)
    return import_plan(envelope, registry, task_input=task), None
  except PlanImportError as e:
    return None, str(e)[:300]
  except Exception as e:  # unreadable/corrupt file
    return None, f"{type(e).__name__}: {e}"


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
  key = _matched_scenario(text)
  if key is None:
    # Conversational gate: only untriggered messages pay this one call —
    # meta/chat turns get a direct answer and never issue a workflow.
    raw = await ctx.run_node(_intent_agent(), node_input=text, run_id="intent")
    verdict = Intent.model_validate(raw)
    if verdict.intent != "data":
      yield _msg(verdict.reply or "Ask me a question about the data!")
      yield _msg(
          "💬 _Conversational turn — no workflow issued (1 intent call,"
          " 0 planner calls, 0 queries)._"
      )
      yield Event(output={"scenario": "conversation", "intent": verdict.intent})
      return
    key = "sequence"
  sc = SCENARIOS[key]
  task = _task_for(
      key,
      text,
      last_insight=ctx.state.get("authored_workflow:ca:last_insight"),
  )
  state_key = f"authored_workflow:ca:{key}"

  if key == "sequence":
    task_note = f' — question: "{task["question"]}"'
  elif key == "adversarial":
    src_note = (
        "canned demo set"
        if task == sc["task"]
        else "YOUR insights (live input)"
    )
    task_note = f" — auditing {src_note}: {task['insights']}"
  else:
    task_note = ""
  data_note = (
      "LIVE `bigquery-public-data.thelook_ecommerce`"
      if _bq_client() is not None
      else "mock `thelook_ecommerce` warehouse (no BigQuery credentials)"
  )
  yield _msg(
      f"🗂️ **Scenario: {sc['title']}** — expected shape `{sc['shape']}`,"
      f" over {data_note}"
      f" ({', '.join(TABLES)}){task_note}."
  )

  # 1. LOAD-OR-AUTHOR. Reuse order: this session's state -> the
  # CROSS-SESSION plan store (defensive import) -> author fresh.
  spec, source = None, None
  existing = ctx.state.get(state_key)
  if existing:
    spec = WorkflowSpec.model_validate(existing)
    source = "session state"
  else:
    spec, reject = _load_stored_plan(key, reg, task)
    if spec is not None:
      source = "plan store (CROSS-SESSION import)"
      ctx.state[state_key] = spec.model_dump()  # cache for this session
    elif reject:
      yield _msg(
          f"🛑 **Plan-store import rejected** for `{key}` — {reject}\n"
          "Drift never silently replays a stale plan; re-authoring fresh."
      )
  if spec is not None:
    spec_hash = _hash(spec)
    reused = True
    fresh_input = task != sc["task"]
    yield _msg(
        f"♻️ **Reusing frozen plan** for `{key}` from {source} — hash"
        f" `{spec_hash}`. The model is NOT re-invoked"
        + (
            "; the import recomputed the hash, re-validated against the"
            " current registry, and checked contract-hash drift"
            if "CROSS-SESSION" in (source or "")
            else ""
        )
        + (
            " — your NEW question is the task input (**template reuse**:"
            " same plan, new data flowing through it)."
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

  # 3. FREEZE (per scenario) + EXPORT (cross-session).
  if not reused:
    ctx.state[state_key] = spec.model_dump()
    record = FrozenWorkflowRecord.freeze(
        spec,
        planner_model=MODEL,
        registry=reg,
        created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        task_input=task,
        # capture the input schema = TEMPLATE promotion: a new session may
        # run this plan on a NEW question, validated against this schema.
        task_input_schema={"required": sorted(task)},
    )
    path = _store_plan(key, record)
    yield _msg(
        f"🔒 **Frozen** under `{state_key}` — hash `{spec_hash}`. 📦"
        f" Exported the full record to `{os.path.relpath(path)}` —"
        " **a NEW session will import and reuse this plan** (defensive"
        " import: hash + registry + contract-hash checks, task input"
        " validated against the captured schema)."
    )

  # 4. EXECUTE on the real engine via the #92 supervisor.
  t0 = time.perf_counter()
  interp = SpecInterpreter(reg, ctx)
  result = await interp.execute(spec, task)
  elapsed = time.perf_counter() - t0
  verdict_lines = _verdict_lines(interp.state)
  if verdict_lines:
    rendered = "\n".join(f"   - {line}" for line in verdict_lines)
    yield _msg(
        "🕵️ **Skeptic verdicts** (one independent skeptic per insight —"
        f" provably isolated from whatever produced it):\n{rendered}"
    )
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
  if isinstance(result, dict) and isinstance(result.get("insight"), str):
    # remembered so a later 'audit that insight' audits THIS, not canned data
    ctx.state["authored_workflow:ca:last_insight"] = result["insight"]
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
