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

"""CI-safe tests for the CA governance demo (no LLM, no BigQuery).

The governance claims are about VALIDATION and MATCHING, which are
deterministic — so the core proofs run with the language capabilities stubbed
and BigQuery forced to the mock warehouse:

* STRICT registry REJECTS an adversarial nl2sql plan (you can't prompt past it);
* a matching question ROUTES to the frozen golden query and runs it;
* a non-matching question REFUSES in strict mode (0 ad-hoc queries);
* FLEXIBLE mode falls back to nl2sql AND promotes the result into the pool;
* after promotion, the same question becomes a governed hit.
"""

from __future__ import annotations

import os
import sys

os.environ["CA_GOV_USE_BIGQUERY"] = "0"  # force the deterministic warehouse

from google.adk import Event
from google.adk.runners import Runner
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.adk.workflow import node
from google.adk import Workflow
from google.genai import types
import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "..", "authored_workflow_spike"))
from authoring import Capability  # noqa: E402
from authoring import CapabilityRegistry  # noqa: E402
from authoring import SpecInterpreter  # noqa: E402
from authoring import SpecValidationError  # noqa: E402
from authoring import WorkflowSpecValidator  # noqa: E402
from bq_ca_governance import agent as demo  # noqa: E402
from bq_ca_governance import golden  # noqa: E402


def _stub(name, fn):
  def build():
    @node(name=name)
    async def n(ctx, node_input):
      yield Event(output=fn(node_input))

    return n

  return build


_VALID_SQL = "SELECT status, COUNT(*) AS orders FROM orders GROUP BY status"


def _stub_registry(mode: str, nl2sql_sql: str = _VALID_SQL) -> CapabilityRegistry:
  """The demo registry for `mode`, with the LLM capabilities stubbed. The
  stubbed nl2sql echoes the question (as the real schema now allows) so the
  promoted record keeps it."""
  real = demo.golden_registry() if mode == "strict" else demo.flexible_registry()
  stubs = {
      "summarize": Capability(
          name="summarize", input_kind="item", serialize_input=False,
          output_model=demo.Summary,
          build=_stub("summarize", lambda v: {"summary": "stub insight."}),
      ),
      "nl2sql": Capability(
          name="nl2sql", input_kind="item", serialize_input=False,
          output_model=demo.Sql,
          build=_stub("nl2sql", lambda v: {
              "sql": nl2sql_sql,
              "question": demo._obj(v).get("question", ""),
          }),
      ),
  }
  caps = [stubs.get(c, real[c]) for c in real.names()]
  return CapabilityRegistry(caps, version=real.version)


async def _run(spec, registry, task):
  holder = {}

  @node(rerun_on_resume=True)
  async def parent(ctx, node_input):
    interp = SpecInterpreter(registry, ctx)
    holder["out"] = await interp.execute(spec, task)
    holder["state"] = dict(interp.state)
    holder["dispatches"] = interp.dispatch_count
    yield Event(output={"_done": True})

  wf = Workflow(name="t", edges=[("START", parent)])
  ss = InMemorySessionService()
  r = Runner(app_name=wf.name, node=wf, session_service=ss)
  s = await ss.create_session(app_name=wf.name, user_id="u")
  async for _ in r.run_async(
      user_id="u", session_id=s.id,
      new_message=types.Content(parts=[types.Part(text="go")], role="user"),
  ):
    pass
  return holder


# ----------------------------------------------------------------- the proofs
def test_strict_registry_rejects_adversarial_nl2sql_plan():
  """The headline: a plan that drafts fresh SQL cannot validate under STRICT."""
  spec = demo.author_adversarial_plan()
  with pytest.raises(SpecValidationError) as e:
    WorkflowSpecValidator(demo.golden_registry()).validate(spec)
  assert "nl2sql" in str(e.value)
  # the SAME plan is fine under flexible -> it's the registry, not the plan.
  assert WorkflowSpecValidator(demo.flexible_registry()).validate(spec) is not None


def test_golden_plan_validates_clean_under_strict():
  warnings = WorkflowSpecValidator(demo.golden_registry()).validate(
      demo.author_golden_plan()
  )
  assert warnings == []


@pytest.mark.asyncio
async def test_matching_question_routes_to_frozen_golden_query():
  h = await _run(
      demo.author_golden_plan(),
      _stub_registry("strict"),
      {"question": "What is total revenue by country?"},
  )
  assert h["out"].get("summary")  # answered, not refused
  assert not h["out"].get("refused")
  run = h["state"]["run"]
  assert run["source"] == "verified"
  assert run["query_id"] == "vq_revenue_by_country"
  assert run["rows"]  # mock warehouse returned rows


@pytest.mark.asyncio
async def test_nonmatching_question_refuses_in_strict():
  h = await _run(
      demo.author_golden_plan(),
      _stub_registry("strict"),
      {"question": "Show customer churn cohorts by signup acquisition channel"},
  )
  assert h["out"].get("refused") is True
  assert "run" not in h["state"]  # no query executed
  assert "deny" in h["state"]


@pytest.mark.asyncio
async def test_flexible_falls_back_validates_and_promotes_with_question(
    tmp_path, monkeypatch
):
  monkeypatch.setenv("CA_GOV_STORE", str(tmp_path))
  q = "What is the average order item sale price by product department?"
  h = await _run(demo.author_flexible_plan(), _stub_registry("flexible"),
                 {"question": q})
  # the gate passed: nl2sql -> dry_run(valid) -> run_adhoc -> freeze -> summarize
  assert h["out"].get("summary")
  assert h["state"]["check"]["valid"] is True
  assert h["state"]["adhoc"]["source"] == "adhoc"
  assert h["state"]["freeze"]["promoted"] is True
  # the promoted record keeps the ORIGINAL question (comment #2 regression).
  assert h["state"]["freeze"]["question"] == q
  pool = golden.load_pool()
  assert any(rec.get("question") == q for rec in pool.values())


@pytest.mark.asyncio
async def test_flexible_gate_rejects_invalid_sql_no_run_no_freeze(
    tmp_path, monkeypatch
):
  """Comment #3: the dry-run is a GATE — invalid generated SQL is neither run
  nor promoted."""
  monkeypatch.setenv("CA_GOV_STORE", str(tmp_path))
  q = "Delete everything please"
  reg = _stub_registry("flexible", nl2sql_sql="DELETE FROM orders")
  h = await _run(demo.author_flexible_plan(), reg, {"question": q})
  assert h["out"].get("refused") is True
  assert h["state"]["check"]["valid"] is False
  assert "adhoc" not in h["state"]  # nothing ran
  assert "freeze" not in h["state"]  # nothing promoted
  assert set(golden.load_pool()) == set(golden._SEED)  # pool unchanged


@pytest.mark.asyncio
async def test_promoted_query_becomes_a_governed_hit(tmp_path, monkeypatch):
  monkeypatch.setenv("CA_GOV_STORE", str(tmp_path))
  q = "How many distinct users placed an order last month?"
  golden.promote(q, "SELECT COUNT(DISTINCT user_id) AS users FROM orders")
  h = await _run(demo.author_golden_plan(), _stub_registry("strict"),
                 {"question": q})
  assert not h["out"].get("refused")
  assert h["state"]["match"]["hit"] is True


def test_registries_clean_and_typed():
  for reg in (demo.golden_registry(), demo.flexible_registry()):
    assert "match_verified_query" in reg
    assert reg.open_map_warnings() == []
  assert "nl2sql" not in demo.golden_registry()
  assert "nl2sql" in demo.flexible_registry()


def test_root_agent_importable_and_named():
  assert demo.root_agent.name == "bq_ca_governance"


def test_seed_golden_queries_match_their_own_questions():
  pool = golden.load_pool()
  for qid, rec in golden._SEED.items():
    m = golden.fallback_match(rec["question"], pool)
    assert m["hit"] and m["query_id"] == qid


def test_mode_routing_is_three_distinct_modes(monkeypatch):
  monkeypatch.delenv("CA_GOV_MODE", raising=False)
  assert demo._mode_from("revenue by country (strict)") == "strict"
  assert demo._mode_from("revenue by country (flexible)") == "flexible"
  assert demo._mode_from("revenue by country (open mode)") == "open"
  assert demo._mode_from("revenue by country") == "strict"  # default
  monkeypatch.setenv("CA_GOV_MODE", "open")
  assert demo._mode_from("revenue by country") == "open"


def test_read_only_guard_blocks_non_select(monkeypatch):
  """Comment #4: DDL/DML and multi-statement SQL are rejected before execution
  (and before the mock), so nothing is billed."""
  from bq_ca_governance import warehouse

  assert warehouse.read_only_violation("SELECT 1") is None
  assert warehouse.read_only_violation(
      "WITH x AS (SELECT 1) SELECT * FROM x") is None
  assert warehouse.read_only_violation("DROP TABLE users")
  assert warehouse.read_only_violation("DELETE FROM orders")
  assert warehouse.read_only_violation("SELECT 1; DELETE FROM orders")
  assert warehouse.read_only_violation("UPDATE orders SET status='x'")
  # the guard is enforced by run_query / dry_run (engine 'guard', not executed)
  assert warehouse.run_query({"sql": "DROP TABLE users"})["engine"] == "guard"
  assert warehouse.dry_run({"sql": "DELETE FROM orders"})["valid"] is False
  assert warehouse.query_thelook("INSERT INTO orders VALUES (1)")["error"]
  # a legitimate read-only query still works against the mock warehouse.
  assert warehouse.run_query(
      {"sql": "SELECT status, COUNT(*) AS orders FROM orders GROUP BY status"}
  )["engine"] == "mock"


def test_mock_dry_run_accepts_cte():
  """Mock dry-run must agree with BigQuery on a legal CTE (a `WITH ... SELECT`
  must not be rejected just because it does not start with `select`)."""
  from bq_ca_governance import warehouse

  out = warehouse.dry_run({"sql": "WITH x AS (SELECT 1 AS n) SELECT * FROM x"})
  assert out["valid"] is True and out["engine"] == "mock"
