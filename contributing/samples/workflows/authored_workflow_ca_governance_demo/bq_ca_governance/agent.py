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

"""Governance demo — golden-query-via-workflow vs. normal agentic response.

One BigQuery Conversational Analytics agent with a **governance dial**, built on
the RFC #93 model-authored-workflow engine. The point it proves to leadership:
*restricting CA to governed ("golden") queries cannot be done with a prompt — it
is enforced structurally by the workflow engine.*

The lever is the engine's own ``CapabilityRegistry`` + ``WorkflowSpecValidator``:
a plan may only compose capabilities in the registry, and the validator
hard-rejects any plan that references one that is not. Governance is therefore a
**registry composition**, not an instruction:

* ``golden_registry`` (STRICT): ``match_verified_query``, ``run_frozen_query``,
  ``summarize``, ``refuse`` — **no ``nl2sql``**. The planner *cannot* author a
  free-SQL step; the capability does not exist for it.
* ``flexible_registry``: STRICT **+** ``nl2sql`` / ``dry_run`` / ``run_adhoc`` /
  ``freeze_verified`` (the constrained-yet-flexible middle ground).

Runtime behavior (one agent, two surfaces):

* a data question is matched against the **verified/golden query pool**; on a
  **hit** it is answered by a frozen, auditable **model-authored workflow** that
  runs the approved SQL on **real BigQuery** (``thelook_ecommerce``);
* on a **miss**, STRICT mode **refuses** (outside the governed set) while OPEN
  mode falls through to a **normal agentic agent** (a real ADK ``Agent`` with a
  ``query_thelook`` BigQuery tool) — today's free-form CA;
* a conversational/meta turn gets a direct agentic reply (no workflow).

Real Gemini calls (intent, summaries, nl2sql, the agentic agent) and real
BigQuery (dry-run + execution). Without credentials it degrades to a
deterministic micro-warehouse, engine-labeled so it never misrepresents itself.

Run:
    export GOOGLE_GENAI_USE_VERTEXAI=1 GOOGLE_CLOUD_PROJECT=<project>
    export GOOGLE_CLOUD_LOCATION=global CA_GOV_MODEL=gemini-3.5-flash
    adk web contributing/samples/workflows/authored_workflow_ca_governance_demo
"""

from __future__ import annotations

import datetime
import json
import os
import sys
from typing import Literal
from typing import Optional

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
from authoring import Binding  # noqa: E402
from authoring import Branch  # noqa: E402
from authoring import Capability  # noqa: E402
from authoring import CapabilityRegistry  # noqa: E402
from authoring import export_plan  # noqa: E402
from authoring import FrozenWorkflowRecord  # noqa: E402
from authoring import independence_facts  # noqa: E402
from authoring import Route  # noqa: E402
from authoring import SpecInterpreter  # noqa: E402
from authoring import SpecValidationError  # noqa: E402
from authoring import StepRef  # noqa: E402
from authoring import WorkflowSpec  # noqa: E402
from authoring import WorkflowSpecValidator  # noqa: E402

from . import golden
from . import warehouse

MODEL = os.environ.get("CA_GOV_MODEL") or os.environ.get(
    "SPIKE_GEMINI_MODEL", "gemini-2.5-flash"
)
DET = types.GenerateContentConfig(temperature=0)


# --------------------------------------------------------------- typed outputs
class Intent(BaseModel):
  intent: Literal["data", "meta"]
  reply: str = ""


class MatchResult(BaseModel):
  hit: bool
  query_id: Optional[str] = None
  sql: Optional[str] = None
  matched_question: Optional[str] = None
  score: float = 0.0
  question: str = ""
  matcher: str = "keyword"


class QueryRows(BaseModel):
  rows: list[dict] = []
  engine: str = "mock"
  sql: str = ""
  question: str = ""
  source: str = ""
  query_id: Optional[str] = None


class Summary(BaseModel):
  summary: str


class Refusal(BaseModel):
  refused: bool
  message: str
  question: str = ""
  score: float = 0.0


class Sql(BaseModel):
  sql: str


class DryRunOut(BaseModel):
  valid: bool
  error: Optional[str] = None
  sql: str = ""
  question: str = ""
  engine: str = "mock"


class Promotion(BaseModel):
  promoted: bool
  query_id: str
  question: str = ""


# --------------------------------------------------------------- value helpers
def _obj(v):
  if isinstance(v, dict):
    return v
  if isinstance(v, str):
    try:
      o = json.loads(v)
      return o if isinstance(o, dict) else {}
    except (ValueError, TypeError):
      return {}
  return {}


def _now_iso() -> str:
  return datetime.datetime.now(datetime.timezone.utc).isoformat()


# --------------------------------------------------------------- capability fns
def _match(value) -> dict:
  question = _obj(value).get("question", "") or (
      value if isinstance(value, str) else ""
  )
  return golden.fallback_match(question, golden.load_pool())


def _run_frozen(value) -> dict:
  m = _obj(value)
  out = warehouse.run_query({"sql": m.get("sql", "")})
  return {
      "rows": out.get("rows", []),
      "engine": out.get("engine"),
      "sql": m.get("sql", ""),
      "question": m.get("question", ""),
      "source": "verified",
      "query_id": m.get("query_id"),
      "matched_question": m.get("matched_question"),
      "error": out.get("error"),
  }


def _refuse(value) -> dict:
  m = _obj(value)
  return {
      "refused": True,
      "message": (
          "This question is outside the governed (verified) query set. In"
          " STRICT mode I only answer from analyst-approved queries to keep"
          " results accurate and costs bounded. Ask an analyst to add a"
          " verified query for it, or switch to OPEN mode."
      ),
      "question": m.get("question", ""),
      "score": m.get("score", 0.0),
  }


def _dry_run(value) -> dict:
  out = warehouse.dry_run(value)
  out["question"] = _obj(value).get("question", "")
  return out


def _run_adhoc(value) -> dict:
  sql = warehouse.sql_of(value)
  out = warehouse.run_query({"sql": sql})
  return {
      "rows": out.get("rows", []),
      "engine": out.get("engine"),
      "sql": sql,
      "question": _obj(value).get("question", ""),
      "source": "adhoc",
      "error": out.get("error"),
  }


def _freeze_verified(value) -> dict:
  m = _obj(value)
  rec = golden.promote(m.get("question", ""), m.get("sql", ""))
  return {"promoted": True, "query_id": rec["id"], "question": m.get("question", "")}


# --------------------------------------------------------------- capabilities
def _node_cap(name, fn, output_model) -> Capability:
  def build():
    @node(name=name)
    async def n(ctx, node_input):
      yield Event(output=fn(node_input))

    return n

  return Capability(
      name=name,
      build=build,
      input_kind="item",
      output_model=output_model,
      serialize_input=False,
  )


def _llm_cap(name, output_model, instruction) -> Capability:
  return Capability(
      name=name,
      build=lambda: Agent(
          name=name,
          model=MODEL,
          output_schema=output_model,
          generate_content_config=DET,
          instruction=instruction,
      ),
      input_kind="item",
      output_model=output_model,
      serialize_input=True,
  )


_NL2SQL_INSTRUCTION = (
    "You translate a natural-language analytics question into ONE read-only"
    " BigQuery StandardSQL SELECT over the thelook_ecommerce dataset (tables:"
    " orders, order_items, products, users). You are SEMANTICS-CONSTRAINED:"
    " use only those tables/columns, always aggregate (GROUP BY / SUM / COUNT),"
    " and never write DML. (In production this step is bound to the dataset's"
    " semantic model / graph so joins and grains are constrained — the RFC's"
    " 'constrained yet flexible' middle ground.) The input is a JSON object"
    " with a 'question' field. Return {\"sql\": <the query>}."
)

_SUMMARIZE_INSTRUCTION = (
    "You are given query result rows as JSON. Write ONE or TWO factual"
    " sentences stating the headline finding (name the top entities and their"
    " values). Do not invent numbers not present in the rows. Return"
    " {\"summary\": <text>}."
)

_INTENT_INSTRUCTION = (
    "Classify the user's message. If it asks for data/metrics/analysis about"
    " the business (revenue, orders, products, customers, trends), intent ="
    " 'data'. If it is chit-chat, a capability question, or meta, intent ="
    " 'meta' and put a brief helpful answer in 'reply'. Return {intent, reply}."
)


def golden_registry() -> CapabilityRegistry:
  """STRICT: only the governed/golden capabilities. No nl2sql exists here."""
  return CapabilityRegistry(
      [
          _node_cap("match_verified_query", _match, MatchResult),
          _node_cap("run_frozen_query", _run_frozen, QueryRows),
          _llm_cap("summarize", Summary, _SUMMARIZE_INSTRUCTION),
          _node_cap("refuse", _refuse, Refusal),
      ],
      version="gov-1",
  )


def flexible_registry() -> CapabilityRegistry:
  """The constrained-yet-flexible middle ground: golden + a gated nl2sql path
  that can also PROMOTE a new query into the governed pool (assisted authoring)."""
  caps = [
      _node_cap("match_verified_query", _match, MatchResult),
      _node_cap("run_frozen_query", _run_frozen, QueryRows),
      _llm_cap("summarize", Summary, _SUMMARIZE_INSTRUCTION),
      _node_cap("refuse", _refuse, Refusal),
      _llm_cap("nl2sql", Sql, _NL2SQL_INSTRUCTION),
      _node_cap("dry_run", _dry_run, DryRunOut),
      _node_cap("run_adhoc", _run_adhoc, QueryRows),
      _node_cap("freeze_verified", _freeze_verified, Promotion),
  ]
  return CapabilityRegistry(caps, version="flex-1")


def _intent_agent() -> Agent:
  return Agent(
      name="intent",
      model=MODEL,
      output_schema=Intent,
      generate_content_config=DET,
      instruction=_INTENT_INSTRUCTION,
  )


def _agentic_agent() -> Agent:
  """The NORMAL agentic CA surface: a free-form ADK Agent with a BigQuery tool.
  Used for OPEN-mode questions with no governed answer. It is NOT a frozen,
  auditable workflow — that is exactly the governance trade-off the demo shows."""
  return Agent(
      name="agentic_ca",
      model=MODEL,
      tools=[warehouse.query_thelook],
      generate_content_config=DET,
      instruction=(
          "You are a BigQuery Conversational Analytics agent for the"
          " thelook_ecommerce dataset (tables: orders, order_items, products,"
          " users). Answer the user's data question. Use the query_thelook tool"
          " to run small read-only aggregate SELECTs and base your answer on the"
          " returned rows. Be concise and cite the numbers."
      ),
  )


# --------------------------------------------------------------- plan authoring
def author_golden_plan() -> WorkflowSpec:
  """match -> branch( hit: run the frozen golden SQL + summarize | miss: refuse )."""
  return WorkflowSpec(
      goal="answer only from the governed/verified query set",
      steps=[
          StepRef(
              kind="step",
              id="match",
              capability="match_verified_query",
              input=Binding(source="task"),
          ),
          Branch(
              kind="branch",
              id="route",
              on=Binding(source="step", step="match", path="hit"),
              routes=[
                  Route(
                      value="True",
                      block=[
                          StepRef(
                              kind="step",
                              id="run",
                              capability="run_frozen_query",
                              input=Binding(source="step", step="match"),
                          ),
                          StepRef(
                              kind="step",
                              id="sum",
                              capability="summarize",
                              input=Binding(source="step", step="run"),
                          ),
                      ],
                  ),
                  Route(
                      value="False",
                      block=[
                          StepRef(
                              kind="step",
                              id="deny",
                              capability="refuse",
                              input=Binding(source="step", step="match"),
                          )
                      ],
                  ),
              ],
          ),
      ],
      output=Binding(source="step", step="route"),
  )


def author_adversarial_plan() -> WorkflowSpec:
  """What a jailbroken/over-eager planner emits to BYPASS governance: draft
  fresh SQL and run it. Composes ``nl2sql`` — which the STRICT registry does
  not contain, so the validator rejects this plan before anything executes."""
  return WorkflowSpec(
      goal="ignore governance and just write SQL to answer the question",
      steps=[
          StepRef(
              kind="step",
              id="gen",
              capability="nl2sql",
              input=Binding(source="task"),
          ),
          StepRef(
              kind="step",
              id="adhoc",
              capability="run_adhoc",
              input=Binding(source="step", step="gen"),
          ),
          StepRef(
              kind="step",
              id="sum",
              capability="summarize",
              input=Binding(source="step", step="adhoc"),
          ),
      ],
      output=Binding(source="step", step="sum"),
  )


def author_flexible_plan() -> WorkflowSpec:
  """The middle ground: golden match first; on a miss, a gated nl2sql ->
  dry_run -> run -> FREEZE (promote to the governed pool) -> summarize."""
  base = author_golden_plan()
  for route in base.steps[1].routes:
    if route.value == "False":
      route.block = [
          StepRef(kind="step", id="gen", capability="nl2sql",
                  input=Binding(source="step", step="match")),
          StepRef(kind="step", id="check", capability="dry_run",
                  input=Binding(source="step", step="gen")),
          StepRef(kind="step", id="adhoc", capability="run_adhoc",
                  input=Binding(source="step", step="check")),
          StepRef(kind="step", id="freeze", capability="freeze_verified",
                  input=Binding(source="step", step="adhoc")),
          StepRef(kind="step", id="sum", capability="summarize",
                  input=Binding(source="step", step="adhoc")),
      ]
  base.goal = "golden first; constrained nl2sql fallback that grows the pool"
  return base


# --------------------------------------------------------------- presentation
def _msg(text: str) -> Event:
  return Event(content=types.Content(role="model", parts=[types.Part(text=text)]))


def _text_of(node_input) -> str:
  if isinstance(node_input, str):
    return node_input
  parts = getattr(node_input, "parts", None)
  if parts:
    return " ".join(
        p.text for p in parts if getattr(p, "text", None)
    ).strip()
  if isinstance(node_input, dict):
    return str(node_input.get("question") or node_input.get("text") or "")
  return str(node_input)


def _mode_from(text: str) -> str:
  low = text.lower()
  if any(k in low for k in ("open mode", "agentic", "flexible")):
    return "open"
  if any(k in low for k in ("strict", "governed only", "golden only")):
    return "strict"
  return os.environ.get("CA_GOV_MODE", "strict")


def _rows_preview(rows: list[dict], n: int = 6) -> str:
  if not rows:
    return "_(no rows)_"
  head = rows[:n]
  cols = list(head[0].keys())
  lines = [" | ".join(cols), " | ".join("---" for _ in cols)]
  for r in head:
    lines.append(" | ".join(str(r.get(c, "")) for c in cols))
  extra = f"\n_…{len(rows) - n} more rows_" if len(rows) > n else ""
  return "\n".join(lines) + extra


# --------------------------------------------------------------- the agent
@node(rerun_on_resume=True)
async def plan_and_run(ctx: Context, node_input):
  text = _text_of(node_input)
  low = text.lower()
  mode = _mode_from(text)

  # --- special beat: registry / mode diff (no model, no query) -------------
  if any(k in low for k in ("registry diff", "compare mode", "show modes",
                            "governance diff")):
    g = golden_registry().names()
    f = flexible_registry().names()
    yield _msg(
        "## 🎛️ Governance is a registry composition, not a prompt\n\n"
        f"**STRICT (golden) registry** — what a plan may compose:\n`{g}`\n\n"
        f"**FLEXIBLE registry**:\n`{f}`\n\n"
        f"The difference is exactly: `{sorted(set(f) - set(g))}`. STRICT has no"
        " `nl2sql`, so the planner *cannot* author a free-SQL step — the"
        " `WorkflowSpecValidator` rejects any plan that references a capability"
        " not in the registry. Flip the dial by swapping the registry; the"
        " model is never trusted to 'stick to golden queries' on its own."
    )
    yield Event(output={"beat": "registry_diff", "strict": g, "flexible": f})
    return

  # --- special beat: the "you can't prompt your way out" proof -------------
  if any(k in low for k in ("adversarial", "force sql", "ignore governance",
                            "just write sql", "bypass")):
    spec = author_adversarial_plan()
    yield _msg(
        "## 🔒 Adversarial planner vs. STRICT governance\n\n"
        "A jailbroken planner authors a plan that **ignores governance and"
        " drafts fresh SQL** (`nl2sql → run_adhoc → summarize`). Validating it"
        " against the STRICT (golden) registry:"
    )
    try:
      WorkflowSpecValidator(golden_registry()).validate(spec)
      yield _msg("⚠️ unexpectedly passed")  # should not happen
    except SpecValidationError as e:
      yield _msg(
          f"❌ **REJECTED before any query runs** — `{e}`\n\nThe `nl2sql`"
          " capability does not exist in the governed registry, so there is no"
          " prompt the model can write to escape the golden set. Governance is"
          " enforced at **validation**, not by instruction."
      )
    # Same plan, flexible registry -> passes (shows it's the REGISTRY, not the plan).
    try:
      WorkflowSpecValidator(flexible_registry()).validate(spec)
      yield _msg(
          "✅ The *same plan* validates under the FLEXIBLE registry (which does"
          " contain `nl2sql`). The control point is the registry you hand the"
          " validator — auditable, not a prompt."
      )
    except SpecValidationError:
      pass
    yield Event(output={"beat": "adversarial_rejected"})
    return

  # --- conversational gate: meta turns get a normal agentic reply ----------
  raw = await ctx.run_node(_intent_agent(), node_input=text, run_id="intent")
  intent = Intent.model_validate(raw if isinstance(raw, dict) else {"intent": "data"})
  if intent.intent != "data":
    yield _msg(intent.reply or "Ask me a question about the data!")
    yield _msg("💬 _Conversational turn — answered agentically, no workflow._")
    yield Event(output={"beat": "conversation"})
    return

  # --- the governed model-authored workflow --------------------------------
  reg = golden_registry()
  spec = author_golden_plan()
  warnings = WorkflowSpecValidator(reg).validate(spec)
  record = FrozenWorkflowRecord.freeze(
      spec, planner_model=MODEL, registry=reg, created_at=_now_iso()
  )
  yield _msg(
      f"## 🗂️ Governed workflow (mode: **{mode.upper()}**)\n\n"
      "The planner authors a typed `WorkflowSpec` over the **golden registry**"
      " — `match_verified_query → branch(hit: run the frozen approved SQL +"
      " summarize | miss: refuse)`."
  )
  yield _msg(
      "✅ **Validated** against the governed registry"
      f" ({'clean' if not warnings else '; '.join(warnings)}).\n"
      f"🔒 **Frozen** — spec_hash `{record.spec_hash[:12]}`,"
      f" {len(export_plan(record))} fields exported (portable, hash-verified,"
      " re-validated on import).\n🧪 "
      + "; ".join(independence_facts(spec)[:2])
  )

  interp = SpecInterpreter(reg, ctx)
  out = await interp.execute(spec, {"question": text})
  match = interp.state.get("match", {})

  if not out.get("refused"):
    rows = interp.state.get("run", {})
    yield _msg(
        f"🎯 **Governed hit** — matched verified query"
        f" `{match.get('query_id')}` (\"{match.get('matched_question')}\","
        f" score {match.get('score')}).\n\n📄 **Result** (engine:"
        f" `{rows.get('engine')}`):\n\n{_rows_preview(rows.get('rows', []))}"
    )
    yield _msg(
        f"📝 {out.get('summary', '')}\n\n📊 _Served by a frozen, auditable"
        f" workflow — {interp.dispatch_count} dispatches, 1 governed query, 0"
        " model-drafted SQL._"
    )
    yield Event(output={"beat": "governed_hit", "query_id": match.get("query_id"),
                        "engine": rows.get("engine")})
    return

  # miss
  if mode != "open":
    yield _msg(
        f"🚫 **Refused (STRICT)** — {out.get('message')}\n\n_(best match score"
        f" {match.get('score')}, below threshold; 0 queries run.)_"
    )
    yield Event(output={"beat": "refused"})
    return

  # OPEN mode: fall through to the NORMAL agentic agent (ungoverned).
  yield _msg(
      "🔓 **No governed query matched — OPEN mode falls through to the normal"
      " agentic agent** (a free-form ADK Agent with a BigQuery tool). This"
      " answer is *not* a frozen, auditable workflow — that is the governance"
      " trade-off."
  )
  ans = await ctx.run_node(_agentic_agent(), node_input=text, run_id="agentic")
  ans_text = ans if isinstance(ans, str) else json.dumps(ans, default=str)
  yield _msg(f"🤖 _agentic answer_: {ans_text}")
  yield _msg(
      "💡 _Assisted authoring_: an analyst can promote this query into the"
      " governed pool (`freeze_verified`), and the next ask becomes a governed"
      " hit served by the workflow above."
  )
  yield Event(output={"beat": "agentic_fallback"})


root_agent = Workflow(
    name="bq_ca_governance",
    edges=[("START", plan_and_run)],
)
