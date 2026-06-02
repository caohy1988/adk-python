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

"""OPTIONAL live end-to-end evidence for the DynamicNodeSupervisor spike.

This is supporting evidence only — NOT part of the deterministic merge gate.
It is skipped unless a real model is explicitly configured via env vars, so it
never runs in CI by accident and contains no hardcoded project/location/model.

Enable with, e.g. (Vertex):
    export SPIKE_LIVE=1
    export GOOGLE_GENAI_USE_VERTEXAI=1
    export GOOGLE_CLOUD_PROJECT=<your-project>
    export GOOGLE_CLOUD_LOCATION=global         # gemini-3.5-flash serves here
    export SPIKE_GEMINI_MODEL=gemini-3.5-flash  # or any flash model you can access

The model is read from ``SPIKE_GEMINI_MODEL`` and **defaults to
``gemini-2.5-flash``** (broadly available in regional Vertex). To use
``gemini-3.5-flash`` set ``SPIKE_GEMINI_MODEL=gemini-3.5-flash`` and
``GOOGLE_CLOUD_LOCATION=global`` (it does not serve from ``us-central1``).

It runs a 2-stage (review -> severity) pipeline over a few snippets, fanned out
concurrently through the supervisor, and asserts a real concurrency speedup.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

_LIVE = os.environ.get("SPIKE_LIVE") == "1" and bool(
    os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GOOGLE_API_KEY")
)

pytestmark = pytest.mark.skipif(
    not _LIVE,
    reason=(
        "live model not configured; set SPIKE_LIVE=1 and model/project env vars"
    ),
)

MODEL = os.environ.get("SPIKE_GEMINI_MODEL", "gemini-2.5-flash")

SNIPPETS = [
    "def login(pw): return pw == 'admin123'  # hardcoded password",
    "query = f\"SELECT * FROM users WHERE id = {request.args['id']}\"",
    "def add(a, b): return a + b",
    "os.system('ping ' + user_supplied_host)",
]


@pytest.mark.asyncio
async def test_live_gemini_pipeline_speedup():
  import os as _os
  import sys as _sys

  from google.adk import Agent
  from google.adk import Context
  from google.adk import Event
  from google.adk import Workflow
  from google.adk.runners import Runner
  from google.adk.sessions.in_memory_session_service import InMemorySessionService
  from google.adk.workflow import node
  from google.genai import types

  _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
  from supervisor import DynamicNodeSupervisor  # noqa: E402

  reviewer = Agent(
      name="reviewer",
      model=MODEL,
      instruction=(
          "You are a security reviewer. The user message is a code "
          "snippet. In ONE short sentence, state the single biggest "
          "security concern, or 'none'."
      ),
  )
  rater = Agent(
      name="rater",
      model=MODEL,
      instruction=(
          "The user message is a security concern. Reply with EXACTLY "
          "one word: CRITICAL, HIGH, MEDIUM, LOW, or NONE."
      ),
  )

  latencies: list[float] = []

  async def timed(coro):
    t = time.perf_counter()
    out = await coro
    latencies.append(time.perf_counter() - t)
    return out

  @node(rerun_on_resume=True)
  async def parent(ctx: Context, node_input):
    sup = DynamicNodeSupervisor(ctx, gate=8)

    async def review(_prev, snippet, i):
      return await timed(
          sup.dispatch(reviewer, node_input=snippet, run_id=f"rev{i}")
      )

    async def rate(concern, snippet, i):
      return await timed(
          sup.dispatch(rater, node_input=str(concern), run_id=f"rate{i}")
      )

    t0 = time.perf_counter()
    res = await sup.pipeline(SNIPPETS, review, rate)
    yield Event(
        output={
            "probe": "live",
            "res": res,
            "wall": time.perf_counter() - t0,
            "sum": sum(latencies),
            "n": len(latencies),
        }
    )

  wf = Workflow(name="live", edges=[("START", parent)])
  ss = InMemorySessionService()
  runner = Runner(app_name=wf.name, node=wf, session_service=ss)
  session = await ss.create_session(app_name=wf.name, user_id="u")
  msg = types.Content(parts=[types.Part(text="go")], role="user")
  out = None
  async for ev in runner.run_async(
      user_id="u", session_id=session.id, new_message=msg
  ):
    if (
        isinstance(ev, Event)
        and isinstance(ev.output, dict)
        and ev.output.get("probe") == "live"
    ):
      out = ev.output

  assert out is not None
  assert out["n"] == len(SNIPPETS) * 2  # 2 real calls per item
  assert len([r for r in out["res"] if r]) == len(SNIPPETS)
  # concurrent pipeline wall-clock is well under the serial sum of call latencies
  assert out["wall"] < out["sum"] * 0.6
  print(
      f"\nlive {MODEL}: {out['n']} calls, wall={out['wall']:.2f}s "
      f"vs serial-sum={out['sum']:.2f}s = {out['sum']/out['wall']:.1f}x"
  )
