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
async def test_flexible_validates_and_runs_but_does_not_autopromote(
    tmp_path, monkeypatch
):
  """FLEXIBLE generates + validates + runs, but the plan has NO promote
  capability — nothing enters the governed pool from the workflow itself."""
  monkeypatch.setenv("CA_GOV_STORE", str(tmp_path))
  q = "What is the average order item sale price by product department?"
  h = await _run(demo.author_flexible_plan(), _stub_registry("flexible"),
                 {"question": q})
  # gate passed: nl2sql -> dry_run(valid) -> run_adhoc -> summarize
  assert h["out"].get("summary")
  assert h["state"]["check"]["valid"] is True
  assert h["state"]["adhoc"]["source"] == "adhoc"
  assert "freeze" not in h["state"]  # no auto-promote step exists
  assert set(golden.load_pool()) == set(golden._SEED)  # pool NOT grown by the run
  assert "freeze_verified" not in demo.flexible_registry()  # model can't self-promote


def test_hitl_approval_promotes_pending_then_reject_clears(tmp_path, monkeypatch):
  """Promotion is human-in-the-loop: a parked candidate enters the pool only on
  approve, and reject discards it."""
  monkeypatch.setenv("CA_GOV_STORE", str(tmp_path))
  q = "What is the average sale price by department?"
  golden.save_pending(q, "SELECT 1")
  assert set(golden.load_pool()) == set(golden._SEED)  # pending != promoted
  # approve -> enters the pool with the original question
  rec = golden.approve_pending()
  assert rec and rec["question"] == q
  assert golden.get_pending() is None
  assert any(r.get("question") == q for r in golden.load_pool().values())
  # a second candidate, this time rejected, leaves the pool unchanged
  before = set(golden.load_pool())
  golden.save_pending("some other question", "SELECT 2")
  golden.clear_pending()
  assert golden.get_pending() is None
  assert set(golden.load_pool()) == before


@pytest.mark.asyncio
async def test_flexible_gate_rejects_invalid_sql_no_run_no_freeze(
    tmp_path, monkeypatch
):
  """The dry-run is a GATE — invalid generated SQL is neither run nor parked."""
  monkeypatch.setenv("CA_GOV_STORE", str(tmp_path))
  q = "Delete everything please"
  reg = _stub_registry("flexible", nl2sql_sql="DELETE FROM orders")
  h = await _run(demo.author_flexible_plan(), reg, {"question": q})
  assert h["out"].get("refused") is True
  assert h["state"]["check"]["valid"] is False
  assert "adhoc" not in h["state"]  # nothing ran
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


def test_strip_mode_cleans_stored_question():
  assert demo._strip_mode("revenue by dept (flexible)") == "revenue by dept"
  assert demo._strip_mode("revenue by dept (Open Mode)") == "revenue by dept"
  assert demo._strip_mode("revenue by dept") == "revenue by dept"


def test_spec_ids_walks_nested_blocks():
  ids = demo._spec_ids(demo.author_flexible_plan())
  assert {"match", "route", "gen", "check", "gate", "adhoc", "fsum", "vreject"} <= ids
  assert {"match", "route", "run", "sum", "deny"} <= demo._spec_ids(
      demo.author_golden_plan())


@pytest.mark.asyncio
async def test_live_authoring_disabled_returns_none(monkeypatch):
  """With CA_GOV_LIVE_PLANNER=0 the planner is skipped (caller uses fallback);
  early-returns before touching ctx, so ctx=None is safe here."""
  monkeypatch.setenv("CA_GOV_LIVE_PLANNER", "0")
  reg = demo.golden_registry()
  spec = await demo._author_live(
      None, reg, demo._golden_plan_instruction(reg), "q", "planner",
      demo._is_golden_shape)
  assert spec is None


def test_shape_predicates_accept_canned_and_reject_cross_mode():
  """Each canned plan is its own expected shape; another mode's plan is not."""
  assert demo._is_golden_shape(demo.author_golden_plan())
  assert demo._is_flexible_shape(demo.author_flexible_plan())
  assert demo._is_adversarial_shape(demo.author_adversarial_plan())
  assert not demo._is_golden_shape(demo.author_flexible_plan())
  assert not demo._is_golden_shape(demo.author_adversarial_plan())
  assert not demo._is_adversarial_shape(demo.author_golden_plan())


def test_offshape_but_registry_valid_plan_fails_the_shape_gate():
  """A plan with all the right ids/capabilities but a different OUTPUT binding is
  still registry-valid — so the old id-presence gate would have accepted it — yet
  it must fail the exact-shape gate so the live label + execution fall back."""
  spec = demo.author_golden_plan()
  spec.output = demo.Binding(source="step", step="match")  # was step 'route'
  demo.WorkflowSpecValidator(demo.golden_registry()).validate(spec)  # still valid
  assert {"match", "route", "run", "sum", "deny"} <= demo._spec_ids(spec)  # ids OK
  assert not demo._is_golden_shape(spec)  # ...but not the narrated shape


@pytest.mark.asyncio
async def test_live_authoring_offshape_plan_falls_back(monkeypatch):
  """With the live planner ON, a registry-valid but off-shape authored plan makes
  `_author_live` return None so the caller honestly uses the canned fallback."""
  monkeypatch.setenv("CA_GOV_LIVE_PLANNER", "1")
  offshape = demo.author_golden_plan()
  offshape.output = demo.Binding(source="step", step="match")

  class _Ctx:
    async def run_node(self, planner, node_input, run_id):
      return offshape.model_dump()

  reg = demo.golden_registry()
  spec = await demo._author_live(
      _Ctx(), reg, demo._golden_plan_instruction(reg), "q", "planner",
      demo._is_golden_shape, attempts=1)
  assert spec is None


def test_planner_instructions_list_only_registry_caps():
  gi = demo._golden_plan_instruction(demo.golden_registry())
  assert "match_verified_query" in gi and "nl2sql" not in gi  # strict catalogue
  fi = demo._flexible_plan_instruction(demo.flexible_registry())
  assert "nl2sql" in fi  # flexible catalogue exposes the gated path


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
