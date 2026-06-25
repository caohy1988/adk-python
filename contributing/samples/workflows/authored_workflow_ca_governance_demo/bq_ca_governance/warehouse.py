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

"""Real BigQuery execution against the public ``thelook_ecommerce`` dataset.

A slim, self-contained BigQuery backend for the governance demo, adapted from
the sibling ``authored_workflow_ca_demo``: ``dry_run`` and ``run_query`` hit the
REAL ``bigquery-public-data.thelook_ecommerce`` dataset (the dataset the
Conversational Analytics docs demo against), billed to ``GOOGLE_CLOUD_PROJECT``,
with safety rails (``maximum_bytes_billed`` per query, a row cap). Without
credentials (or with ``CA_GOV_USE_BIGQUERY=0``) it falls back to a deterministic
micro-warehouse so CI and credential-less machines keep working — every result
carries an ``engine`` field (``bigquery`` or ``mock``) so the demo never
misrepresents its data source.
"""

from __future__ import annotations

import json
import os
import re
from typing import Optional

DATASET = "bigquery-public-data.thelook_ecommerce"
_MAX_BYTES_BILLED = 2 * 1024**3  # 2 GB per query
_MAX_ROWS = 500

_BQ = {
    "client": None,
    "disabled": os.environ.get("CA_GOV_USE_BIGQUERY", "1") != "1",
    "error": None,
}


def bq_available() -> bool:
  return _client() is not None


def engine_label() -> str:
  return "bigquery" if bq_available() else "mock"


def _client():
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


# ---------------------------------------------------------------- sql helpers
def sql_of(value) -> str:
  """The SQL text from an {'sql': ...} dict, a JSON string, or a raw string."""
  if isinstance(value, dict):
    return str(value.get("sql", ""))
  if isinstance(value, str):
    try:
      obj = json.loads(value)
      if isinstance(obj, dict):
        return str(obj.get("sql", ""))
    except (ValueError, TypeError):
      pass
    return value
  return ""


# Forbidden even when the statement happens to start with SELECT/WITH (e.g.
# scripting, or DML hidden after a CTE). Enforced before BigQuery AND before the
# mock, so the guard is exercised in tests without credentials.
_FORBIDDEN = re.compile(
    r"(?i)\b(insert|update|delete|merge|drop|create|alter|truncate|grant|"
    r"revoke|call|load|export|begin|declare|set)\b"
)


def read_only_violation(sql) -> Optional[str]:
  """Return a reason string if the SQL is not a single read-only SELECT/WITH
  query, else None. Governance + cost safety: OPEN mode lets a model pass
  arbitrary SQL, so DDL/DML, scripting, and multi-statement input are rejected
  before anything is billed to GOOGLE_CLOUD_PROJECT."""
  raw = sql_of(sql)
  # strip full-line comments, then a trailing semicolon/whitespace.
  body = "\n".join(
      ln for ln in (raw or "").splitlines() if not ln.strip().startswith("--")
  ).strip().rstrip(";").strip()
  if not body:
    return "empty SQL"
  if ";" in body:
    return "multiple statements are not allowed (single SELECT only)"
  low = body.lower()
  if not (low.startswith("select") or low.startswith("with")):
    return "only read-only SELECT/WITH queries are allowed"
  if _FORBIDDEN.search(body):
    return "DDL/DML/scripting keywords are not allowed in a read-only query"
  return None


def _qualify(sql: str) -> str:
  """Fully qualify bare thelook table refs for real BigQuery."""
  s = (sql or "").replace("`", "")
  s = re.sub(r"(?<![\w.-])thelook_ecommerce\.", f"{DATASET}.", s)
  return re.sub(
      rf"{re.escape(DATASET)}\.([A-Za-z_]\w*)", rf"`{DATASET}.\1`", s
  )


def _jsonify(v):
  import datetime as _dt
  import decimal

  if isinstance(v, decimal.Decimal):
    return round(float(v), 2)
  if isinstance(v, float):
    return round(v, 2)
  if isinstance(v, (_dt.datetime, _dt.date)):
    return v.isoformat()
  return v


# ---------------------------------------------------------------- public API
def dry_run(value) -> dict:
  """Validate SQL without running it. Real BigQuery dry-run when credentials
  allow (real errors, real bytes); otherwise a cheap syntactic check."""
  violation = read_only_violation(value)
  if violation:
    return {"sql": sql_of(value), "valid": False,
            "error": f"rejected: {violation}", "engine": "guard"}
  sql = _qualify(sql_of(value))
  client = _client()
  if client is None:
    # The read-only guard above already confirmed a single SELECT/WITH query,
    # so the mock dry-run must agree with what BigQuery would accept — including
    # legal CTEs. (Don't re-check for a leading `select`: that would reject a
    # valid `WITH ... SELECT` and diverge from the live backend.)
    return {"sql": sql, "valid": True, "error": None, "engine": "mock"}
  from google.cloud import bigquery

  try:
    job = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False),
    )
    return {
        "sql": sql,
        "valid": True,
        "error": None,
        "engine": "bigquery",
        "bytes_processed": int(job.total_bytes_processed or 0),
    }
  except Exception as e:  # the REAL BigQuery error
    return {"sql": sql, "valid": False, "error": str(e)[:500], "engine": "bigquery"}


def run_query(value) -> dict:
  """Execute a read-only SELECT. Real BigQuery (billed, capped) when
  credentials allow; the deterministic micro-warehouse otherwise."""
  violation = read_only_violation(value)
  if violation:
    return {"rows": [], "engine": "guard", "error": f"rejected: {violation}"}
  sql = _qualify(sql_of(value))
  client = _client()
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
          {k: _jsonify(v) for k, v in dict(r).items()}
          for r in job.result(max_results=_MAX_ROWS)
      ]
      return {
          "rows": rows,
          "engine": "bigquery",
          "bytes_processed": int(job.total_bytes_processed or 0),
      }
    except Exception as e:
      # A failing query must NOT fabricate an answer from the mock — that
      # path is only for missing credentials. Return the failure honestly.
      return {"rows": [], "engine": "bigquery", "error": str(e)[:300]}
  return {"rows": _mock_engine(sql), "engine": "mock"}


def query_thelook(sql: str) -> dict:
  """Run ONE read-only StandardSQL SELECT against
  bigquery-public-data.thelook_ecommerce and return rows. Use small aggregate
  queries (GROUP BY / COUNT / SUM); results are capped. Returns rows, the
  executing engine, and the real error when the SQL is invalid.

  This is the tool the *agentic* (ungoverned) path uses to answer a question
  that has no matching verified/golden query.
  """
  out = run_query({"sql": sql})
  return {
      "rows": out.get("rows", [])[:50],
      "engine": out.get("engine"),
      "error": out.get("error"),
  }


# ----------------------------------------------- deterministic mock warehouse
# Used ONLY without credentials (engine-labeled "mock"). A tiny synthetic fact
# table aggregated by the SQL's intent — enough to keep the shapes alive in CI.
_REGIONS = {"China": 2.74, "United States": 1.83, "Brasil": 1.18, "South Korea": 0.41}
_CATS = {"Outerwear & Coats": 1.00, "Jeans": 0.92, "Sweaters": 0.62, "Swim": 0.48}
_STATUSES = {"Shipped": 37342, "Complete": 31176, "Processing": 24836,
             "Cancelled": 18745, "Returned": 12591}


def _mock_engine(sql: str) -> list[dict]:
  s = (sql or "").lower()
  if "status" in s and "count" in s:
    return [{"status": k, "orders": v} for k, v in _STATUSES.items()]
  if "category" in s:
    return [
        {"category": k, "revenue": round(v * 1_000_000, 2)}
        for k, v in _CATS.items()
    ]
  if "country" in s or "region" in s:
    return [
        {"country": k, "revenue": round(v * 1_000_000, 2)}
        for k, v in _REGIONS.items()
    ]
  if "format_timestamp" in s or "month" in s:
    return [
        {"month": f"2024-{m:02d}", "revenue": round(140000 + m * 2500.0, 2)}
        for m in range(1, 13)
    ]
  return [{"revenue": 6_170_000.0}]
