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

"""Frozen Plan Inspector — renders the plan store as a self-contained HTML page.

Reads every ``FrozenWorkflowRecord`` envelope in ``ca_plan_store/`` and writes
``ca_plan_store/plan_inspector.html``: the plan's dataflow as a diagram, and
every envelope field annotated with the guarantee it delivers (auditability,
tamper evidence, version/contract drift detection, cross-session template
reuse). Run from the repo root after a demo session has frozen some plans:

    python contributing/samples/workflows/authored_workflow_ca_demo/plan_inspector.py
    open ca_plan_store/plan_inspector.html
"""

from __future__ import annotations

import html
import json
import os
import sys

STORE = os.path.join(os.getcwd(), "ca_plan_store")

CSS = """
:root { --ink:#1a1c1e; --mut:#5f6368; --line:#dadce0; --blue:#1a73e8;
        --green:#188038; --amber:#b06000; --purple:#7627bb; --red:#c5221f;
        --bg:#f8f9fa; --card:#ffffff; }
* { box-sizing: border-box; }
body { font: 14px/1.55 -apple-system, 'Segoe UI', Roboto, sans-serif;
       color: var(--ink); background: var(--bg); margin: 0; padding: 32px; }
h1 { font-size: 24px; margin: 0 0 4px; }
h2 { font-size: 18px; margin: 36px 0 10px; }
.sub { color: var(--mut); margin-bottom: 24px; }
.benefits { display: flex; gap: 12px; flex-wrap: wrap; margin: 18px 0 8px; }
.benefit { flex: 1 1 220px; background: var(--card); border: 1px solid var(--line);
           border-radius: 10px; padding: 14px 16px; }
.benefit b { display: block; margin-bottom: 4px; }
.b-audit b { color: var(--blue); } .b-ver b { color: var(--purple); }
.b-cons b { color: var(--green); } .b-safe b { color: var(--amber); }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 12px;
        padding: 20px 22px; margin: 14px 0; }
.tag { display: inline-block; font-size: 11px; font-weight: 600; border-radius: 99px;
       padding: 1px 9px; margin-left: 8px; vertical-align: 2px; }
.t-audit { background:#e8f0fe; color: var(--blue); }
.t-ver   { background:#f3e8fd; color: var(--purple); }
.t-cons  { background:#e6f4ea; color: var(--green); }
.t-safe  { background:#fef7e0; color: var(--amber); }
.kv { margin: 6px 0; padding: 8px 10px; border-left: 3px solid var(--line);
      background: var(--bg); border-radius: 0 6px 6px 0; }
.kv code { font: 12px/1.5 ui-monospace, Menlo, monospace; word-break: break-all; }
.kv .why { color: var(--mut); font-size: 12.5px; margin-top: 2px; }
.flow { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin: 14px 0; }
.node { border: 1.5px solid var(--blue); border-radius: 8px; padding: 7px 12px;
        background: #e8f0fe; font: 12px ui-monospace, Menlo, monospace; }
.node small { display: block; color: var(--mut); font-size: 10.5px; }
.loopbox { border: 1.5px dashed var(--purple); border-radius: 10px; padding: 10px;
           display: flex; gap: 10px; align-items: center; }
.loopbox .lbl { color: var(--purple); font-size: 11px; font-weight: 700; }
.fanbox { border: 1.5px dashed var(--green); border-radius: 10px; padding: 10px; }
.fanbox .lbl { color: var(--green); font-size: 11px; font-weight: 700; }
.arrow { color: var(--mut); font-size: 18px; }
pre { background: #202124; color: #e8eaed; border-radius: 10px; padding: 14px;
      overflow: auto; font: 11.5px/1.5 ui-monospace, Menlo, monospace; max-height: 340px; }
.story { border-left: 4px solid var(--green); background: #e6f4ea55; padding: 10px 14px;
         border-radius: 0 8px 8px 0; margin: 10px 0; }
.bad { border-left-color: var(--red); background: #fce8e655; }
.bad b { color: var(--red); }
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
"""


def _node(step) -> str:
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
        _node(s) for s in step.get("body", [])
    )
    return (
        f'<div class="loopbox"><span class="lbl">LOOP until'
        f" {html.escape(step.get('until_capability', '?'))} (max"
        f" {step.get('max_iters')})</span>{body}</div>"
    )
  if kind == "branch":
    return f'<div class="node">branch: {html.escape(step["id"])}</div>'
  return f'<div class="node">{html.escape(str(kind))}</div>'


def _flow(spec) -> str:
  steps = " <span class='arrow'>→</span> ".join(
      _node(s) for s in spec.get("steps", [])
  )
  return f'<div class="flow">{steps}</div>'


def _kv(label, value, why, tag, tag_label) -> str:
  return (
      f'<div class="kv"><b>{html.escape(label)}</b>'
      f'<span class="tag {tag}">{tag_label}</span><br>'
      f"<code>{html.escape(value)}</code>"
      f'<div class="why">{html.escape(why)}</div></div>'
  )


def _plan_card(name: str, env: dict) -> str:
  spec = env.get("spec", {})
  caps = ", ".join(sorted(env.get("capability_versions", {})))
  ch = env.get("capability_contract_hashes", {})
  ch_short = {k: v[:12] for k, v in ch.items()}
  parts = [
      f'<div class="card"><h2 style="margin-top:0">{html.escape(name)} — '
      f"“{html.escape(spec.get('goal', ''))}”</h2>",
      (
          "<b>The plan, as data</b> — every box is a pre-approved capability;"
          " every arrow is a typed binding the validator checked:"
      ),
      _flow(spec),
      _kv(
          "spec_hash",
          env.get("spec_hash", ""),
          "Tamper evidence: every import recomputes sha256 over the spec and"
          " rejects on mismatch. Change one character of the plan and it"
          " will not load.",
          "t-audit",
          "AUDITABLE",
      ),
      _kv(
          "planner_model · created_at",
          f"{env.get('planner_model')} · {env.get('created_at')}",
          "Provenance: which model authored this plan and when — the audit"
          " trail starts at authoring, not at execution.",
          "t-audit",
          "AUDITABLE",
      ),
      _kv(
          "registry_version · capability_versions",
          f"registry v{env.get('registry_version')} · "
          + json.dumps(env.get("capability_versions", {})),
          "Versioning: the exact capability versions this plan was approved"
          " against. The skeptic at v2 means a v1-era audit plan is REJECTED"
          " on import and re-authored — semantics changed, so the plan must"
          " too.",
          "t-ver",
          "VERSIONED",
      ),
      _kv(
          "capability_contract_hashes",
          json.dumps(ch_short),
          "Drift detection without developer discipline: derived sha256 over"
          " each capability's declared contract. A schema change nobody"
          " version-bumped still refuses to load.",
          "t-ver",
          "VERSIONED",
      ),
      _kv(
          "task_input_schema · task_input_digest",
          f"{json.dumps(env.get('task_input_schema'))} · "
          + str(env.get("task_input_digest", ""))[:16],
          "Consistency across sessions: a NEW session imports this plan and"
          " runs a NEW question through it (validated against the captured"
          " schema) — same steps, same checks, same shape of answer. Zero"
          " planner calls.",
          "t-cons",
          "CONSISTENT",
      ),
      _kv(
          "validation",
          json.dumps(env.get("validation", {})),
          "Recorded but NEVER trusted: import re-validates against the"
          " current registry. Lint waivers, if any, are recorded here too —"
          " suppression is auditable.",
          "t-safe",
          "SAFE",
      ),
      (
          f"<details><summary>Full envelope JSON ({caps})</summary>"
          f"<pre>{html.escape(json.dumps(env, indent=2))}</pre></details>"
      ),
      "</div>",
  ]
  return "\n".join(parts)


def _sql_card(rec: dict) -> str:
  revs = rec.get("revisions", [])
  rev_rows = "".join(
      f'<div class="kv"><b>revision #{i + 1}</b>'
      f'<span class="tag t-audit">HUMAN FEEDBACK</span><br>'
      f"<code>{html.escape(r.get('feedback', ''))}</code>"
      '<div class="why">revised'
      f' {html.escape(str(r.get("revised_at", ""))[:19])}'
      f" — previous SQL preserved in the artifact:</div>"
      "<pre"
      f" style='max-height:120px'>{html.escape(r.get('previous_sql') or '')}</pre></div>"
      for i, r in enumerate(revs)
  )
  return (
      '<div class="card"><h2 style="margin-top:0">🧊 Frozen SQL — '
      f"“{html.escape(rec.get('question', ''))}”</h2>"
      "<b>The numbers, pinned</b> — replays of this exact question skip the"
      " drafting LLM and run THIS statement (re-validated by a real dry-run"
      " first, which doubles as warehouse-drift detection):"
      f"<pre>{html.escape(rec.get('sql', ''))}</pre>"
      + _kv(
          "sql_hash · validated_at · engine",
          f"{rec.get('sql_hash', '')[:16]} ·"
          f" {str(rec.get('validated_at', ''))[:19]} ·"
          f" {rec.get('engine', '')}",
          "Numeric determinism: identical SQL means identical results on an"
          " unchanged dataset — live-verified to the cent across runs and"
          " sessions.",
          "t-cons",
          "CONSISTENT",
      )
      + (
          rev_rows
          or '<div class="kv"><b>revisions</b><br><code>none yet</code><div class="why">say “revise: &lt;feedback&gt;” in the demo — the change must pass the real dry-run before it lands, and the feedback is recorded here forever.</div></div>'
      )
      + "</div>"
  )


def _fetch_session(app: str, user: str, session_id: str, port: int = 8001):
  import urllib.request

  url = f"http://127.0.0.1:{port}/apps/{app}/users/{user}/sessions/{session_id}"
  try:
    with urllib.request.urlopen(url, timeout=5) as r:
      return json.loads(r.read())
  except Exception:
    return None


def _session_timeline(session: dict) -> str:
  """Render the session's ACTUAL flow: one card per user turn, classified by
  the mechanism that answered it, linked to the artifact it touched."""
  import re

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
          '<span class="tag t-cons">🧊 FROZEN-SQL REPLAY</span> drafting LLM'
          " skipped — deterministic numbers"
      )
    elif "Revising the frozen SQL" in beats:
      mech = (
          '<span class="tag t-audit">🛠 HUMAN REVISION</span> feedback'
          " validated by a REAL dry-run, recorded in the artifact, then"
          " executed"
      )
    elif "Authored plan" in beats:
      mech = '<span class="tag t-ver">📝 AUTHORED FRESH</span> 1 planner call'
    elif "Reusing frozen plan" in beats:
      mech = (
          '<span class="tag t-cons">♻️ FROZEN-PLAN REUSE</span> 0 planner calls'
      )
    elif "Conversational turn" in beats:
      mech = (
          '<span class="tag t-safe">💬 CONVERSATION</span> no workflow issued'
      )
    else:
      mech = '<span class="tag t-safe">WORKFLOW</span>'
    hash_m = re.search(r"validated SQL \(hash `([0-9a-f]+)`", beats)
    rev_m = re.search(r"(\d+) human revision", beats)
    extra = ""
    if hash_m:
      extra += f" · sql `{hash_m.group(1)}`"
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
      '<div class="card"><h2 style="margin-top:0">▶️ This session, as it'
      " actually ran</h2><div class='sub'>Read straight from the live ADK"
      " session — each turn classified by the mechanism that answered it."
      " The artifacts those turns created and reused are below.</div>"
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
  sql_cards = ""
  if os.path.isdir(sql_dir):
    for fn in sorted(os.listdir(sql_dir)):
      if fn.endswith(".json"):
        with open(os.path.join(sql_dir, fn)) as f:
          sql_cards += _sql_card(json.load(f))

  timeline = ""
  if len(sys.argv) > 1:
    sid = sys.argv[1]
    app = sys.argv[2] if len(sys.argv) > 2 else "bq_ca_planner"
    user = sys.argv[3] if len(sys.argv) > 3 else "user"
    session = _fetch_session(app, user, sid)
    if session:
      timeline = _session_timeline(session)

  cards = (
      timeline
      + sql_cards
      + "\n".join(_plan_card(k, v) for k, v in envs.items())
  )
  page = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Frozen Plan Inspector — RFC #93</title><style>{CSS}</style></head><body>
<h1>Frozen Plan Inspector</h1>
<div class="sub">The live contents of <code>ca_plan_store/</code> — each file is a
<b>FrozenWorkflowRecord</b>: a model-authored workflow, frozen as a durable, portable artifact.
This page explains what each field buys you.</div>

<div class="benefits">
<div class="benefit b-audit"><b>🔍 Auditable</b>The plan is data you can read, diff in a PR,
and hand to a reviewer — who authored it, when, with which model, and exactly what runs in
what order with what inputs. Turn-by-turn agent chatter leaves no such artifact.</div>
<div class="benefit b-ver"><b>🏷️ Versioned</b>Registry + per-capability versions and derived
contract hashes are sealed in. Capability changed since freezing? The plan refuses to load —
loudly — instead of silently running stale semantics.</div>
<div class="benefit b-cons"><b>♻️ Consistent</b>Authoring is the only nondeterministic step,
and it happens once. Every session that imports this plan executes the same steps with the
same safety checks — only the data changes. Answers stop depending on the model's mood.</div>
<div class="benefit b-safe"><b>🛡️ Safe</b>Closed capability vocabulary, typed bindings,
plan-quality lints, fail-closed defensive import. No model-written code is ever stored or
executed.</div>
<div class="benefit b-cons"><b>🧊 Numerically deterministic</b>SQL freezing extends the
freeze one level deeper: a question's dry-run-validated SQL becomes part of the artifact,
so replays skip the drafting LLM entirely — same numbers, to the cent, across runs and
sessions. Human feedback amends it through validation, with every revision recorded.</div>
</div>

<div class="story"><b>The consistency story in one line:</b> Session A authored these plans
(1 planner call each). Session B — tomorrow, another user, another machine — imports them,
re-validates them, and runs new questions through them with <b>0 planner calls</b>: the
same governed pipeline every time.</div>
<div class="story bad"><b>The tamper story in one line:</b> edit one character of any
<code>spec</code> below and the next import fails with <code>spec_hash mismatch</code>;
change a capability's schema and it fails with <code>contract drift</code>. Drift never
replays silently.</div>

{cards}
</body></html>"""

  out = os.path.join(STORE, "plan_inspector.html")
  with open(out, "w") as f:
    f.write(page)
  print(out)
  return out


if __name__ == "__main__":
  main()
