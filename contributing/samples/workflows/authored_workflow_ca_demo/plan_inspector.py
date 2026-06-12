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

"""RFC #93 evidence page — frozen workflows with frozen middle results.

Renders the plan store as a self-contained HTML pitch for the RFC: the
model-authored typed plan is the centerpiece; validated INTERMEDIATE step
results (here: the dry-run-checked SQL the drafting loop produced) freeze
onto the step that produced them, so replays skip that step's LLM entirely;
human feedback amends the frozen artifact through validation, with every
revision recorded. SQL is the demonstrated instance — the mechanism is the
RFC's general step-result freezing tier.

    python plan_inspector.py [session-id] [app] [user]
    open ca_plan_store/plan_inspector.html

With a session id, the page opens with that live session's actual flow,
each turn classified by the RFC mechanism that answered it.
"""

from __future__ import annotations

import html
import json
import os
import re
import sys

STORE = os.path.join(os.getcwd(), "ca_plan_store")

CSS = """
:root { --ink:#1a1c1e; --mut:#5f6368; --line:#dadce0; --blue:#1a73e8;
        --green:#188038; --amber:#b06000; --purple:#7627bb; --red:#c5221f;
        --ice:#0277bd; --bg:#f8f9fa; --card:#ffffff; }
* { box-sizing: border-box; }
body { font: 14px/1.55 -apple-system, 'Segoe UI', Roboto, sans-serif;
       color: var(--ink); background: var(--bg); margin: 0; padding: 32px; }
h1 { font-size: 24px; margin: 0 0 4px; }
h2 { font-size: 18px; margin: 36px 0 10px; }
h3 { font-size: 14px; margin: 18px 0 6px; }
.sub { color: var(--mut); margin-bottom: 18px; }
.pitch { border-left: 4px solid var(--blue); background: #e8f0fe66;
         padding: 12px 16px; border-radius: 0 10px 10px 0; margin: 14px 0; }
.claims { display: flex; gap: 12px; flex-wrap: wrap; margin: 18px 0 8px; }
.claim { flex: 1 1 210px; background: var(--card); border: 1px solid var(--line);
         border-radius: 10px; padding: 13px 15px; }
.claim b { display: block; margin-bottom: 4px; }
.c1 b { color: var(--blue); } .c2 b { color: var(--purple); }
.c3 b { color: var(--ice); } .c4 b { color: var(--green); } .c5 b { color: var(--amber); }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 12px;
        padding: 20px 22px; margin: 14px 0; }
.tag { display: inline-block; font-size: 11px; font-weight: 600; border-radius: 99px;
       padding: 1px 9px; margin-left: 8px; vertical-align: 2px; }
.t-audit { background:#e8f0fe; color: var(--blue); }
.t-ver   { background:#f3e8fd; color: var(--purple); }
.t-cons  { background:#e6f4ea; color: var(--green); }
.t-safe  { background:#fef7e0; color: var(--amber); }
.t-ice   { background:#e1f5fe; color: var(--ice); }
.kv { margin: 6px 0; padding: 8px 10px; border-left: 3px solid var(--line);
      background: var(--bg); border-radius: 0 6px 6px 0; }
.kv code { font: 12px/1.5 ui-monospace, Menlo, monospace; word-break: break-all; }
.kv .why { color: var(--mut); font-size: 12.5px; margin-top: 2px; }
.flow { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin: 14px 0; }
.node { border: 1.5px solid var(--blue); border-radius: 8px; padding: 7px 12px;
        background: #e8f0fe; font: 12px ui-monospace, Menlo, monospace; }
.node small { display: block; color: var(--mut); font-size: 10.5px; }
.loopbox { border: 1.5px dashed var(--purple); border-radius: 10px;
           padding: 14px 10px 10px; display: flex; gap: 10px;
           align-items: center; position: relative; }
.loopbox .lbl { color: var(--purple); font-size: 11px; font-weight: 700; }
.loopbox.iced { border-color: var(--ice); background: #e1f5fe33; }
.loopbox.iced::after { content: "❄️ result frozen — SKIPPED on replay";
        position: absolute; top: -11px; right: 10px; font-size: 10px;
        background: var(--ice); color: #fff; border-radius: 99px;
        padding: 1px 8px; }
.fanbox { border: 1.5px dashed var(--green); border-radius: 10px; padding: 10px; }
.fanbox .lbl { color: var(--green); font-size: 11px; font-weight: 700; }
.arrow { color: var(--mut); font-size: 18px; }
pre { background: #202124; color: #e8eaed; border-radius: 10px; padding: 14px;
      overflow: auto; font: 11.5px/1.5 ui-monospace, Menlo, monospace; max-height: 300px; }
details summary { cursor: pointer; color: var(--blue); font-weight: 600; margin: 8px 0; }
.turn { display: flex; gap: 14px; margin: 10px 0; }
.turn .num { flex: 0 0 30px; height: 30px; border-radius: 50%; background: var(--blue);
             color: #fff; font-weight: 700; display: flex; align-items: center;
             justify-content: center; }
.turn .body { flex: 1; background: var(--card); border: 1px solid var(--line);
              border-radius: 10px; padding: 10px 14px; }
.turn .ask { font-weight: 600; }
.turn .mech { font-size: 12px; margin: 4px 0; }
.turn .insight { color: var(--mut); font-size: 12.5px; border-left: 3px solid var(--line);
                 padding-left: 8px; margin-top: 6px; }
.midresult { border: 1.5px solid var(--ice); border-radius: 10px;
             background: #e1f5fe44; padding: 12px 14px; margin: 10px 0; }
.midresult .q { font-weight: 600; }
.rev { border-left: 3px solid var(--blue); background: var(--bg); padding: 8px 10px;
       border-radius: 0 6px 6px 0; margin: 6px 0; font-size: 12.5px; }
"""

_SQL_PRODUCER_HINTS = ("draft", "sqlgen", "sql", "loop")


def _node(step, frozen_step_ids=()) -> str:
  kind = step.get("kind")
  if kind == "step":
    binding = step.get("input", {})
    src = (
        "task input"
        if binding.get("source") == "task"
        else f"← {binding.get('step')}"
    )
    return (
        f'<div class="node">{html.escape(step["id"])}'
        f"<small>{html.escape(step['capability'])} ·"
        f" {html.escape(src)}</small></div>"
    )
  if kind == "fan_out":
    over = step.get("over", {})
    src = over.get("path") or over.get("step") or "task"
    inner = (
        f'<div class="node">{html.escape(step["id"])}'
        f"<small>{html.escape(step['capability'])} × each of"
        f" {html.escape(str(src))}</small></div>"
    )
    return (
        '<div class="fanbox"><span class="lbl">FAN-OUT (parallel,'
        f" isolated)</span>{inner}</div>"
    )
  if kind == "pipeline":
    stages = " <span class='arrow'>→</span> ".join(
        f'<div class="node">{html.escape(s["capability"])}</div>'
        for s in step.get("stages", [])
    )
    return (
        f'<div class="fanbox"><span class="lbl">PIPELINE (per item,'
        f" barrier-free)</span><div class='flow'>{stages}</div></div>"
    )
  if kind == "loop_until":
    body = " <span class='arrow'>→</span> ".join(
        _node(s, frozen_step_ids) for s in step.get("body", [])
    )
    iced = " iced" if step.get("id") in frozen_step_ids else ""
    return (
        f'<div class="loopbox{iced}"><span class="lbl">LOOP until'
        f" {html.escape(step.get('until_capability', '?'))} (max"
        f" {step.get('max_iters')})</span>{body}</div>"
    )
  if kind == "branch":
    return f'<div class="node">branch: {html.escape(step["id"])}</div>'
  return f'<div class="node">{html.escape(str(kind))}</div>'


def _flow(spec, frozen_step_ids=()) -> str:
  steps = " <span class='arrow'>→</span> ".join(
      _node(s, frozen_step_ids) for s in spec.get("steps", [])
  )
  return f'<div class="flow">{steps}</div>'


def _kv(label, value, why, tag, tag_label) -> str:
  return (
      f'<div class="kv"><b>{html.escape(label)}</b>'
      f'<span class="tag {tag}">{tag_label}</span><br>'
      f"<code>{html.escape(value)}</code>"
      f'<div class="why">{html.escape(why)}</div></div>'
  )


def _sql_producing_step_ids(spec) -> list:
  """The plan steps whose validated results the store freezes (the drafting
  loop in the ask-a-question plan)."""
  ids = []
  for s in spec.get("steps", []):
    sid = str(s.get("id", "")).lower()
    if s.get("kind") == "loop_until" and any(
        h in sid for h in _SQL_PRODUCER_HINTS
    ):
      ids.append(s.get("id"))
  return ids


def _mid_result(rec: dict) -> str:
  """A frozen middle result, attached to the plan that produced it."""
  revs = rec.get("revisions", [])
  rev_html = "".join(
      f'<div class="rev"><b>revision #{i + 1} — human feedback:</b>'
      f" {html.escape(r.get('feedback', ''))}"
      f"<details><summary>previous artifact (preserved)</summary>"
      f"<pre>{html.escape(r.get('previous_sql') or '')}</pre></details></div>"
      for i, r in enumerate(revs)
  )
  return (
      '<div class="midresult"><div class="q">❄️ Frozen middle result —'
      f" question: “{html.escape(rec.get('question', ''))}”</div>"
      f"<div style='font-size:12px;color:var(--mut)'>artifact hash"
      f" <code>{rec.get('sql_hash', '')[:16]}</code> · validated"
      f" {str(rec.get('validated_at', ''))[:19]} ·"
      f" engine {html.escape(str(rec.get('engine', '')))} ·"
      f" {len(revs)} human revision(s)</div>"
      f"<details><summary>the validated artifact (SQL, in this instance)"
      f"</summary><pre>{html.escape(rec.get('sql', ''))}</pre></details>"
      + (rev_html or "")
      + "</div>"
  )


def _plan_card(name: str, env: dict, mid_results=()) -> str:
  spec = env.get("spec", {})
  frozen_ids = _sql_producing_step_ids(spec) if mid_results else []
  parts = [
      f'<div class="card"><h2 style="margin-top:0">Frozen workflow:'
      f" {html.escape(name)} — “{html.escape(spec.get('goal', ''))}”</h2>",
      (
          "<b>Authored by the model ONCE, as typed data</b> — every box a"
          " pre-approved capability, every arrow a typed binding the"
          " validator checked. The plan replays across sessions with zero"
          " planner calls:"
      ),
      _flow(spec, frozen_ids),
  ]
  if mid_results:
    parts.append(
        "<h3>❄️ Frozen middle results of this workflow</h3>"
        "<div class='sub' style='margin-bottom:6px'>The RFC's step-result"
        " freezing tier: the ❄️ step's validated output is frozen WITH the"
        " plan. On replay the step's LLM is skipped — the run is"
        " numerically deterministic — and the artifact re-validates on"
        " load (drift detection). Human feedback amends it THROUGH"
        " validation, every revision recorded:</div>"
    )
    parts.extend(_mid_result(r) for r in mid_results)
  parts += [
      _kv(
          "spec_hash",
          env.get("spec_hash", ""),
          "Tamper evidence: every import recomputes sha256 over the spec"
          " and rejects on mismatch.",
          "t-audit",
          "AUDITABLE",
      ),
      _kv(
          "planner_model · created_at",
          f"{env.get('planner_model')} · {env.get('created_at')}",
          "Authoring provenance: which model wrote this orchestration and"
          " when.",
          "t-audit",
          "AUDITABLE",
      ),
      _kv(
          "registry + capability versions · contract hashes",
          f"registry v{env.get('registry_version')} · "
          + json.dumps(env.get("capability_versions", {}))
          + " · "
          + json.dumps({
              k: v[:10]
              for k, v in (env.get("capability_contract_hashes") or {}).items()
          }),
          "Drift detection: a capability whose contract changed since"
          " freezing makes the plan refuse to load — loudly.",
          "t-ver",
          "VERSIONED",
      ),
      _kv(
          "task_input_schema · task_input_digest",
          f"{json.dumps(env.get('task_input_schema'))} · "
          + str(env.get("task_input_digest", ""))[:16],
          "Template reuse: a new session validates ITS question against"
          " the captured schema and runs the same governed pipeline.",
          "t-cons",
          "CONSISTENT",
      ),
      (
          "<details><summary>full frozen record (envelope JSON)</summary>"
          f"<pre>{html.escape(json.dumps(env, indent=2))}</pre></details>"
      ),
      "</div>",
  ]
  return "\n".join(parts)


def _fetch_session(app: str, user: str, session_id: str, port: int = 8001):
  import urllib.request

  url = f"http://127.0.0.1:{port}/apps/{app}/users/{user}/sessions/{session_id}"
  try:
    with urllib.request.urlopen(url, timeout=5) as r:
      return json.loads(r.read())
  except Exception:
    return None


def _session_timeline(session: dict) -> str:
  turns, cur = [], None
  for e in session.get("events", []):
    content = e.get("content") or {}
    texts = [
        p.get("text", "") for p in content.get("parts") or [] if p.get("text")
    ]
    blob = " ".join(texts)
    if e.get("author") == "user" and blob.strip():
      cur = {"ask": blob.strip(), "beats": []}
      turns.append(cur)
    elif cur is not None and blob:
      cur["beats"].append(blob)

  cards = []
  for i, t in enumerate(turns, 1):
    beats = " ".join(t["beats"])
    if "Frozen SQL replay" in beats:
      mech = (
          '<span class="tag t-ice">❄️ STEP-RESULT REPLAY</span> the'
          " workflow ran with its drafting step SKIPPED — the frozen"
          " middle result reused; numbers deterministic"
      )
    elif "Revising the frozen SQL" in beats:
      mech = (
          '<span class="tag t-audit">🛠 HUMAN-GOVERNED REVISION</span>'
          " feedback applied to the frozen middle result THROUGH a real"
          " dry-run, recorded in the artifact, then executed"
      )
    elif "Authored plan" in beats:
      mech = (
          '<span class="tag t-ver">📝 MODEL AUTHORED THE WORKFLOW</span>'
          " once — typed plan, validated, frozen (1 planner call)"
      )
    elif "Reusing frozen plan" in beats:
      mech = (
          '<span class="tag t-cons">♻️ FROZEN-WORKFLOW REPLAY</span> 0'
          " planner calls — new data through the same governed pipeline"
      )
    elif "Conversational turn" in beats:
      mech = (
          '<span class="tag t-safe">💬 CONVERSATION</span> intent gate —'
          " no workflow issued"
      )
    else:
      mech = '<span class="tag t-safe">WORKFLOW</span>'
    hash_m = re.search(r"validated SQL \(hash `([0-9a-f]+)`", beats)
    rev_m = re.search(r"(\d+) human revision", beats)
    extra = ""
    if hash_m:
      extra += f" · artifact `{hash_m.group(1)}`"
    if rev_m:
      extra += f" · {rev_m.group(1)} revision(s) applied"
    ins_m = re.search(r'"insight": "([^"]+)', beats)
    insight = (
        f'<div class="insight">{html.escape(ins_m.group(1)[:220])}</div>'
        if ins_m
        else ""
    )
    cards.append(
        f'<div class="turn"><div class="num">{i}</div><div class="body">'
        f'<div class="ask">“{html.escape(t["ask"][:160])}”</div>'
        f'<div class="mech">{mech}{html.escape(extra)}</div>{insight}'
        "</div></div>"
    )
  if not cards:
    return ""
  return (
      '<div class="card"><h2 style="margin-top:0">▶️ The mechanism, live —'
      " this session as it actually ran</h2><div class='sub'>Read straight"
      " from the running ADK session. Watch the arc: the workflow answers"
      " → a human amends its frozen middle result → the SAME workflow"
      " replays carrying the revision.</div>"
      + "".join(cards)
      + "</div>"
  )


def main() -> str:
  envs = {}
  for fn in sorted(os.listdir(STORE)):
    if fn.endswith(".json"):
      with open(os.path.join(STORE, fn)) as f:
        envs[fn[:-5]] = json.load(f)
  if not envs:
    print("plan store is empty — run a demo session first", file=sys.stderr)
    raise SystemExit(1)
  sql_dir = os.path.join(STORE, "sql")
  mid_results = []
  if os.path.isdir(sql_dir):
    for fn in sorted(os.listdir(sql_dir)):
      if fn.endswith(".json"):
        with open(os.path.join(sql_dir, fn)) as f:
          mid_results.append(json.load(f))

  timeline = ""
  if len(sys.argv) > 1:
    sid = sys.argv[1]
    app = sys.argv[2] if len(sys.argv) > 2 else "bq_ca_planner"
    user = sys.argv[3] if len(sys.argv) > 3 else "user"
    session = _fetch_session(app, user, sid)
    if session:
      timeline = _session_timeline(session)

  # middle results attach to the workflow that produced them (sequence).
  cards = timeline
  for name, env in envs.items():
    attach = mid_results if name == "sequence" else ()
    cards += _plan_card(name, env, attach)

  page = f"""<!doctype html><html><head><meta charset="utf-8">
<title>RFC #93 — Frozen Workflows with Frozen Middle Results</title>
<style>{CSS}</style></head><body>
<h1>RFC #93: Reproducible Model-Authored Workflows</h1>
<div class="sub">Demonstrated live on BigQuery Conversational Analytics over
<code>bigquery-public-data.thelook_ecommerce</code> — every artifact on this page is real,
read from the running demo's plan store.</div>

<div class="pitch"><b>The thesis:</b> a model should author orchestration <b>once</b>, as
typed data — then the workflow, and the validated <b>middle results its steps produce</b>,
freeze into durable artifacts. Replays skip the nondeterministic steps entirely; humans
amend the artifacts through validation, never by re-prompting; and everything is
auditable, versioned, and drift-checked. A chat agent gives you answers. This gives you a
<b>governed analytics asset</b>.</div>

<div class="claims">
<div class="claim c1"><b>📝 Authored once</b>The model emits a typed plan over a closed
capability vocabulary — no code, no sandbox. Validated, lint-checked, frozen, exported.</div>
<div class="claim c2"><b>🏷️ Versioned &amp; drift-checked</b>Registry, capability versions,
and derived contract hashes seal in. Changed semantics → the plan refuses to load.</div>
<div class="claim c3"><b>❄️ Middle results freeze too</b>The step that drafts SQL is the last
nondeterministic step — so its dry-run-validated output freezes WITH the plan. Replays skip
it: same numbers, to the cent, across sessions.</div>
<div class="claim c4"><b>🛠 Human-governed</b>Feedback amends a frozen middle result through
real validation; the feedback and the previous artifact are preserved in the record — a
reviewed change, not a re-roll.</div>
<div class="claim c5"><b>🔍 Auditable end to end</b>Who authored the plan, what it runs,
which artifact answered, who revised it and why — all readable, diffable data.</div>
</div>

{cards}

<div class="card"><h2 style="margin-top:0">Why this matters beyond SQL</h2>
What froze here is a SQL statement — but the mechanism is general: <b>any step's validated
output</b> can freeze the same way. A retrieved schema, a verified claim set, a chart
specification, an extraction template — each one a middle result that today is re-rolled by
an LLM on every run. The RFC's freezing tiers turn them into governed artifacts:
<b>v1</b> the frozen plan (process determinism) · <b>v1.1</b> the exported envelope
(portability + audit) · <b>v1.2</b> frozen step results (numeric determinism + human
governance) · <b>v2</b> templates (approved reuse against new inputs).</div>
</body></html>"""

  out = os.path.join(STORE, "plan_inspector.html")
  with open(out, "w") as f:
    f.write(page)
  print(out)
  return out


if __name__ == "__main__":
  main()
