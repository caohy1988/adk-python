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

"""The verified-query ("golden query") pool — the governed answer set.

A verified query is deterministic SQL an analyst has approved: it executes when
a user's question matches it, instead of letting a model draft fresh SQL. This
mirrors BigQuery Conversational Analytics' *verified queries* feature (the
renamed "golden queries"). The pool is the unit of governance: STRICT mode can
answer ONLY from it.

Seed queries are real, schema-grounded SQL against
``bigquery-public-data.thelook_ecommerce`` (validated to execute). The pool is
file-backed (``CA_GOV_STORE/verified/*.json``) so the *assisted-authoring* loop
can promote a new analyst-approved query into it at runtime — growing the
governed set over time.
"""

from __future__ import annotations

import json
import os
import re
from typing import Optional

_D = "bigquery-public-data.thelook_ecommerce"

# id -> {question, keywords, sql}. SQL validated against the real dataset.
_SEED: dict[str, dict] = {
    "vq_revenue_by_country": {
        "question": "What is total revenue by country?",
        "keywords": ["revenue", "country", "sales", "by country", "geography"],
        "sql": (
            f"SELECT u.country, ROUND(SUM(oi.sale_price), 2) AS revenue\n"
            f"FROM `{_D}.order_items` oi\n"
            f"JOIN `{_D}.users` u ON oi.user_id = u.id\n"
            "WHERE oi.status NOT IN ('Cancelled', 'Returned')\n"
            "GROUP BY u.country ORDER BY revenue DESC LIMIT 10"
        ),
    },
    "vq_top_categories": {
        "question": "What are the top product categories by revenue?",
        "keywords": ["top", "category", "categories", "product", "revenue", "best selling"],
        "sql": (
            f"SELECT p.category, ROUND(SUM(oi.sale_price), 2) AS revenue\n"
            f"FROM `{_D}.order_items` oi\n"
            f"JOIN `{_D}.products` p ON oi.product_id = p.id\n"
            "WHERE oi.status NOT IN ('Cancelled', 'Returned')\n"
            "GROUP BY p.category ORDER BY revenue DESC LIMIT 10"
        ),
    },
    "vq_orders_by_status": {
        "question": "How many orders are in each status?",
        "keywords": ["orders", "status", "count", "how many", "fulfillment"],
        "sql": (
            f"SELECT status, COUNT(*) AS orders\n"
            f"FROM `{_D}.orders`\n"
            "GROUP BY status ORDER BY orders DESC"
        ),
    },
    "vq_monthly_revenue": {
        "question": "What is the monthly revenue trend?",
        "keywords": ["monthly", "trend", "revenue", "over time", "by month"],
        "sql": (
            "SELECT FORMAT_TIMESTAMP('%Y-%m', oi.created_at) AS month,\n"
            "       ROUND(SUM(oi.sale_price), 2) AS revenue\n"
            f"FROM `{_D}.order_items` oi\n"
            "WHERE oi.status NOT IN ('Cancelled', 'Returned')\n"
            "GROUP BY month ORDER BY month"
        ),
    },
}


def _store_dir() -> str:
  base = os.environ.get(
      "CA_GOV_STORE",
      os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ca_gov_store"),
  )
  d = os.path.join(base, "verified")
  os.makedirs(d, exist_ok=True)
  return d


def load_pool() -> dict[str, dict]:
  """The seed pool merged with any runtime-promoted (file-backed) queries."""
  pool = {k: dict(v) for k, v in _SEED.items()}
  d = _store_dir()
  for fname in sorted(os.listdir(d)):
    if fname.endswith(".json"):
      try:
        with open(os.path.join(d, fname)) as f:
          rec = json.load(f)
        pool[rec["id"]] = rec
      except (OSError, ValueError, KeyError):
        continue
  return pool


def promote(question: str, sql: str) -> dict:
  """Assisted authoring: add an analyst-approved query to the governed pool."""
  qid = "vq_" + re.sub(r"[^a-z0-9]+", "_", question.lower()).strip("_")[:48]
  rec = {
      "id": qid,
      "question": question,
      "keywords": sorted(set(re.findall(r"[a-z]+", question.lower()))),
      "sql": sql,
  }
  with open(os.path.join(_store_dir(), qid + ".json"), "w") as f:
    json.dump(rec, f, indent=1)
  return rec


# --------------------------------------------------- human-in-the-loop (HITL)
# A FLEXIBLE-generated, dry-run-validated query is NOT written to the governed
# pool automatically — there is no promote capability in the registry, so the
# model cannot self-promote. The validated candidate is parked here; a human
# must explicitly `approve` it before it becomes a verified/golden query.
# Single-slot by design (one candidate awaiting sign-off at a time).
_PENDING = "pending_candidate.json"


def _pending_path() -> str:
  base = os.environ.get(
      "CA_GOV_STORE",
      os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ca_gov_store"),
  )
  os.makedirs(base, exist_ok=True)
  return os.path.join(base, _PENDING)


def save_pending(question: str, sql: str) -> dict:
  """Park a validated candidate awaiting human approval."""
  rec = {"question": question, "sql": sql}
  with open(_pending_path(), "w") as f:
    json.dump(rec, f, indent=1)
  return rec


def get_pending() -> Optional[dict]:
  try:
    with open(_pending_path()) as f:
      return json.load(f)
  except (OSError, ValueError):
    return None


def clear_pending() -> None:
  try:
    os.remove(_pending_path())
  except OSError:
    pass


def approve_pending() -> Optional[dict]:
  """Human sign-off: move the pending candidate into the governed pool."""
  rec = get_pending()
  if rec is None:
    return None
  promoted = promote(rec["question"], rec["sql"])
  clear_pending()
  return promoted


_MATCH_MIN_OVERLAP = 2  # need >= 2 distinct keyword hits to count as governed


def fallback_match(question: str, pool: dict[str, dict]) -> dict:
  """Deterministic keyword-overlap match — the no-LLM / CI matcher and the
  safety net behind a semantic (LLM/embedding) matcher. A question matches a
  verified query when it shares at least ``_MATCH_MIN_OVERLAP`` distinct
  keyword tokens; the best-overlap query wins. Returns a MatchResult dict."""
  q = set(re.findall(r"[a-z]+", (question or "").lower()))
  best_id, best_overlap = None, 0
  for qid, e in pool.items():
    kw = set()
    for k in e.get("keywords", []):
      kw.update(re.findall(r"[a-z]+", k.lower()))
    overlap = len(q & kw)
    if overlap > best_overlap:
      best_id, best_overlap = qid, overlap
  hit = best_overlap >= _MATCH_MIN_OVERLAP
  return {
      "hit": hit,
      "query_id": best_id if hit else None,
      "sql": pool[best_id]["sql"] if hit else None,
      "matched_question": pool[best_id]["question"] if hit else None,
      "score": best_overlap,
      "question": question,
      "matcher": "keyword",
  }
