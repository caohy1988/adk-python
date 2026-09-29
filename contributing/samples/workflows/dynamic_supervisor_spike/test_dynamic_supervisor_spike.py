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

"""Deterministic regression harness for the DynamicNodeSupervisor spike.

These tests use deterministic FunctionNodes (no LLM) and run against the REAL
ADK Workflow engine (Runner + InMemorySessionService). They are the artifact
that makes the RFC credible: they pin exactly which properties hold.

Expected result: ALL PASS. (First captured on ADK 2.0.0; re-verified on the
branch rebased onto current upstream main.)
All five merge-gate properties hold with a wrapper supervisor on the unmodified
engine — barrier-free execution, pipeline barrier-free, failed-item isolation,
control-exception cancellation, nested no-deadlock (+ driver-gating deadlock
contrast), and resume exactly-once for children that COMPLETE before an
interrupt (both sequential and concurrent). The only documented behavior is a
design trade-off, not a bug: a child that interrupts while siblings are still
IN FLIGHT causes those siblings to be cancelled and re-run on resume.

(An earlier draft reported a concurrent-resume "engine gap"; that was a test
artifact — the interrupt fired before siblings completed, so they were
cancelled, not completed. It has been retracted.)
"""

from __future__ import annotations

import asyncio
import collections
import os
import sys
import time

from google.adk import Context
from google.adk import Event
from google.adk import Workflow
from google.adk.apps.app import App
from google.adk.apps.app import ResumabilityConfig
from google.adk.events.request_input import RequestInput
from google.adk.runners import Runner
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.adk.workflow import node
from google.adk.workflow._errors import NodeInterruptedError
from google.adk.workflow.utils._workflow_hitl_utils import create_request_input_response
from google.adk.workflow.utils._workflow_hitl_utils import get_request_input_interrupt_ids
from google.adk.workflow.utils._workflow_hitl_utils import REQUEST_INPUT_FUNCTION_CALL_NAME
from google.genai import types
import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from supervisor import DynamicNodeSupervisor  # noqa: E402  (local spike module)


# --------------------------------------------------------------------------
# Harness helpers
# --------------------------------------------------------------------------
async def _run(parent_fn, *, app_name="spike"):
  wf = Workflow(name=app_name, edges=[("START", parent_fn)])
  ss = InMemorySessionService()
  runner = Runner(app_name=wf.name, node=wf, session_service=ss)
  session = await ss.create_session(app_name=wf.name, user_id="u")
  msg = types.Content(parts=[types.Part(text="go")], role="user")
  probes = []
  async for ev in runner.run_async(
      user_id="u", session_id=session.id, new_message=msg
  ):
    if (
        isinstance(ev, Event)
        and isinstance(ev.output, dict)
        and "probe" in ev.output
    ):
      probes.append(ev.output)
  return probes


def _child(name, delay=0.0, fail=None, log=None):
  @node(name=name)
  async def child(ctx, node_input):
    if log is not None:
      log.append((name, "start", time.perf_counter()))
    await asyncio.sleep(delay)
    if fail == "error":
      raise ValueError(f"{name} boom")
    if log is not None:
      log.append((name, "end", time.perf_counter()))
    yield Event(output=f"{name}<-{node_input}")

  return child


# --------------------------------------------------------------------------
# 1. Concurrent dispatch executes correctly and barrier-free
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_concurrent_dispatch_correct_and_barrier_free():
  log = []

  @node(rerun_on_resume=True)
  async def parent(ctx: Context, node_input):
    sup = DynamicNodeSupervisor(ctx)
    delays = [0.05, 0.05, 0.05, 0.05]
    res = await sup.parallel([
        (
            lambda i=i, d=delays[i]: sup.dispatch(
                _child(f"c{i}", d, log=log), node_input=i, run_id=f"r{i}"
            )
        )
        for i in range(4)
    ])
    # peak_in_flight is the primary, timing-independent proof of concurrency.
    yield Event(output={"probe": "bf", "res": res, "peak": sup.peak_in_flight})

  out = (await _run(parent))[0]
  assert sorted(out["res"]) == [
      f"c{i}<-{i}" for i in range(4)
  ]  # correct + distinct
  assert len(set(out["res"])) == 4  # no aliasing / corruption
  assert out["peak"] == 4  # all 4 truly ran at once
  # event-order overlap: every child starts before any child ends (true fan-out)
  starts = sorted(t for (_, p, t) in log if p == "start")
  ends = sorted(t for (_, p, t) in log if p == "end")
  assert max(starts) < min(ends)  # all started before any ended


# --------------------------------------------------------------------------
# 2. pipeline barrier-free: item0 enters stage2 before item1 finishes stage1
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_pipeline_barrier_free():
  log = []

  @node(rerun_on_resume=True)
  async def parent(ctx: Context, node_input):
    sup = DynamicNodeSupervisor(ctx)

    async def s1(prev, item, i):
      return await sup.dispatch(
          _child(f"s1_{i}", 0.25 if i == 1 else 0.0, log=log),
          node_input=item,
          run_id=f"s1x{i}",
      )

    async def s2(prev, item, i):
      return await sup.dispatch(
          _child(f"s2_{i}", 0.0, log=log), node_input=prev, run_id=f"s2x{i}"
      )

    res = await sup.pipeline([0, 1], s1, s2)
    yield Event(output={"probe": "pf", "res": res})

  await _run(parent)
  starts = {n: t for (n, p, t) in log if p == "start"}
  ends = {n: t for (n, p, t) in log if p == "end"}
  assert "s2_0" in starts and "s1_1" in ends
  assert starts["s2_0"] < ends["s1_1"]  # no inter-stage barrier


# --------------------------------------------------------------------------
# 3. parallel failed-item isolation: ordinary error -> None, siblings fine
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_parallel_failed_item_isolation():
  @node(rerun_on_resume=True)
  async def parent(ctx: Context, node_input):
    sup = DynamicNodeSupervisor(ctx)
    res = await sup.parallel([
        (lambda: sup.dispatch(_child("p0", 0.02), node_input=0, run_id="p0")),
        (
            lambda: sup.dispatch(
                _child("p1", 0.01, fail="error"), node_input=1, run_id="p1"
            )
        ),
        (lambda: sup.dispatch(_child("p2", 0.02), node_input=2, run_id="p2")),
    ])
    yield Event(output={"probe": "fi", "res": res})

  res = (await _run(parent))[0]["res"]
  assert res == ["p0<-0", None, "p2<-2"]


# --------------------------------------------------------------------------
# 4. Supervisor fan-out contract: ordinary -> None; control exception
#    PROPAGATES and CANCELS siblings. Requires TaskGroup (gather would not
#    cancel). Tested directly on the supervisor (no engine needed).
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_control_exception_propagates_and_cancels_siblings():
  sup = DynamicNodeSupervisor(ctx=None)
  cancelled = {"v": False}

  async def boom():
    raise ValueError("ordinary")

  async def interrupt():
    raise NodeInterruptedError()

  async def sibling():
    try:
      await asyncio.sleep(1.0)
      return "finished"
    except asyncio.CancelledError:
      cancelled["v"] = True
      raise

  async def okk():
    return "ok"

  # ordinary error -> None; sibling unaffected
  assert await sup.parallel([lambda: boom(), lambda: okk()]) == [None, "ok"]

  # control exception propagates AND cancels the running sibling
  with pytest.raises(NodeInterruptedError):
    await sup.parallel([lambda: interrupt(), lambda: sibling()])
  await asyncio.sleep(0)  # let cancellation settle
  assert cancelled["v"] is True  # explicit sibling-cancellation assertion


@pytest.mark.asyncio
async def test_child_cancellederror_does_not_cancel_siblings():
  """Contract boundary (narrowed): a branch raising asyncio.CancelledError is
  asyncio's own task-cancellation. TaskGroup does NOT propagate it and does NOT
  cancel siblings — the branch's slot is left None and siblings complete. This
  is asyncio semantics; the supervisor does not (and is not claimed to) override
  it. Only NodeInterruptedError / non-cancellation BaseException cancel siblings.
  """
  sup = DynamicNodeSupervisor(ctx=None)
  sib_finished = {"v": False}

  async def canceller():
    raise asyncio.CancelledError()

  async def sibling():
    await asyncio.sleep(0.03)
    sib_finished["v"] = True
    return "sib-done"

  res = await sup.parallel([lambda: canceller(), lambda: sibling()])
  assert res == [
      None,
      "sib-done",
  ]  # cancelled branch -> None; sibling NOT cancelled
  assert sib_finished["v"] is True


def test_gate_must_be_positive():
  """gate=0 would deadlock every dispatch; reject it at construction."""
  with pytest.raises(ValueError):
    DynamicNodeSupervisor(ctx=None, gate=0)
  with pytest.raises(ValueError):
    DynamicNodeSupervisor(ctx=None, gate=-1)


# --------------------------------------------------------------------------
# 5. Nested combinator no-deadlock with LEAF gating (gate=2).
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_nested_combinator_no_deadlock_leaf_gating():
  @node(rerun_on_resume=True)
  async def parent(ctx: Context, node_input):
    sup = DynamicNodeSupervisor(ctx, gate=2)

    async def stage(prev, item, i):
      return await sup.parallel([
          (
              lambda k=k: sup.dispatch(
                  _child(f"n{item}_{k}", 0.02),
                  node_input=k,
                  run_id=f"n{item}x{k}",
              )
          )
          for k in range(3)
      ])

    res = await sup.pipeline(list(range(5)), stage)
    yield Event(
        output={"probe": "nest", "n": len(res), "peak": sup.peak_in_flight}
    )

  out = await asyncio.wait_for(_run(parent), timeout=10.0)  # must NOT hang
  assert out[0]["n"] == 5
  assert out[0]["peak"] <= 2  # leaf-gating bounds in-flight to the gate


# --------------------------------------------------------------------------
# 5b. CONTRAST: gating DRIVERS deadlocks on nesting (proves leaf-gating matters)
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_driver_gating_deadlocks_as_predicted():
  @node(rerun_on_resume=True)
  async def parent(ctx: Context, node_input):
    sup = DynamicNodeSupervisor(ctx, gate=2)

    async def stage(prev, item, i):
      return await sup.parallel([
          (
              lambda k=k: sup.dispatch(
                  _child(f"d{item}_{k}", 0.02),
                  node_input=k,
                  run_id=f"d{item}x{k}",
              )
          )
          for k in range(3)
      ])

    res = await sup.pipeline(list(range(5)), stage, gate_drivers=True)  # BUGGY
    yield Event(output={"probe": "dead", "n": len(res)})

  with pytest.raises(asyncio.TimeoutError):
    await asyncio.wait_for(_run(parent), timeout=3.0)


# --------------------------------------------------------------------------
# Resume exactly-once — the merge gate.
#
# CORRECTION (vs an earlier draft of this harness): there is NO resume engine
# gap. An earlier test let the RequestInput child interrupt *before* its
# siblings finished, so the TaskGroup CANCELLED still-running siblings; those
# cancelled (never-completed) children then re-ran on resume — which is
# correct, not a bug. The tests below separate the two cases cleanly:
#   * children that COMPLETE before the interrupt -> fast-forward (exactly-once)
#   * children still IN FLIGHT at the interrupt -> cancelled -> correctly re-run
# Both hold for sequential AND concurrent dispatch.
# --------------------------------------------------------------------------
async def _resume_scenario(*, concurrent, ask_delay, child_delay):
  """Dispatch 3 plain children + 1 RequestInput child, interrupt, resume.

  Returns (body_runs, completed) where body_runs[name] counts body ENTRIES and
  `completed` lists children that ran to completion (emitted output) on run 1.
  The counter is captured by closure (NOT a pydantic field) so every body
  execution is observed by the same object.

  Timing knobs decide whether children complete before the interrupt:
    ask_delay   -- ask sleeps this long before issuing RequestInput
    child_delay -- each plain child sleeps this long before completing
  """
  body_runs = collections.Counter()
  completed = []

  def plain(name):
    @node(name=name)
    async def child(ctx, node_input):
      body_runs[name] += 1
      await asyncio.sleep(child_delay)
      completed.append(name)
      yield Event(output=f"{name}=done")

    return child

  @node(name="ask", rerun_on_resume=True)
  async def ask(ctx: Context, node_input):
    body_runs["ask"] += 1
    resume = getattr(ctx, "resume_inputs", {}).get("ask")
    if resume is None:
      await asyncio.sleep(ask_delay)
      yield RequestInput(interrupt_id="ask", message="approve ask?")
    else:
      yield Event(output="ask=approved")

  @node(rerun_on_resume=True)
  async def parent(ctx: Context, node_input):
    sup = DynamicNodeSupervisor(ctx, gate=8)
    thunks = [
        (lambda: sup.dispatch(plain("a"), node_input=1, run_id="ax")),
        (lambda: sup.dispatch(plain("b"), node_input=2, run_id="bx")),
        (lambda: sup.dispatch(plain("c"), node_input=3, run_id="cx")),
        (lambda: sup.dispatch(ask, node_input=4, run_id="askx")),
    ]
    if concurrent:
      res = await sup.parallel(thunks)
    else:
      res = [await t() for t in thunks]  # sequential control
    yield Event(output={"probe": "resume", "res": res})

  wf = Workflow(name="resume_wf", edges=[("START", parent)])
  app = App(
      name="resume_app",
      root_agent=wf,
      resumability_config=ResumabilityConfig(is_resumable=True),
  )
  ss = InMemorySessionService()
  runner = Runner(app=app, session_service=ss)
  session = await ss.create_session(app_name=app.name, user_id="u")

  # run 1 -> expect RequestInput interrupt
  msg = types.Content(parts=[types.Part(text="go")], role="user")
  ev1 = [
      e
      async for e in runner.run_async(
          user_id="u", session_id=session.id, new_message=msg
      )
  ]
  req = None
  for e in ev1:
    if getattr(e, "content", None) and e.content and e.content.parts:
      for p in e.content.parts:
        if (
            p.function_call
            and p.function_call.name == REQUEST_INPUT_FUNCTION_CALL_NAME
        ):
          req = e
  assert req is not None, "expected a RequestInput interrupt on run 1"
  completed_on_run1 = list(completed)
  interrupt_id = get_request_input_interrupt_ids(req)[0]
  invocation_id = req.invocation_id

  # resume
  part = create_request_input_response(interrupt_id, {"approved": "yes"})
  _ = [
      e
      async for e in runner.run_async(
          user_id="u",
          session_id=session.id,
          new_message=types.Content(parts=[part], role="user"),
          invocation_id=invocation_id,
      )
  ]
  return body_runs, completed_on_run1


@pytest.mark.asyncio
async def test_sequential_resume_is_exactly_once():
  """Baseline: sequential dispatch — children complete in order before ask."""
  runs, completed1 = await _resume_scenario(
      concurrent=False, ask_delay=0.0, child_delay=0.0
  )
  assert set(completed1) == {"a", "b", "c"}  # all completed on run 1
  assert (
      runs["a"] == 1 and runs["b"] == 1 and runs["c"] == 1
  )  # fast-forward on resume
  assert runs["ask"] == 2  # interrupted node re-runs


@pytest.mark.asyncio
async def test_concurrent_resume_completed_children_fast_forward():
  """Merge gate: under CONCURRENT dispatch, children that COMPLETE before the
  interrupt fast-forward on resume (exactly-once). ask sleeps so a/b/c finish
  first."""
  runs, completed1 = await _resume_scenario(
      concurrent=True, ask_delay=0.10, child_delay=0.0
  )
  assert set(completed1) == {"a", "b", "c"}  # genuinely completed
  assert (
      runs["a"] == 1 and runs["b"] == 1 and runs["c"] == 1
  )  # NOT re-run -> exactly-once
  assert runs["ask"] == 2


@pytest.mark.asyncio
async def test_concurrent_inflight_children_cancelled_on_interrupt_rerun():
  """Documents the one real behavior: under CONCURRENT dispatch, a sibling that
  interrupts while others are still IN FLIGHT causes the TaskGroup to cancel
  them. Cancelled (never-completed) children correctly re-run on resume. This
  is correctness-preserving (not a double-spend of completed work), though it
  does discard the cancelled siblings' partial progress — a design trade-off
  the RFC should note."""
  runs, completed1 = await _resume_scenario(
      concurrent=True, ask_delay=0.0, child_delay=0.10
  )
  assert completed1 == []  # none completed (all cancelled)
  assert (
      runs["a"] == 2 and runs["b"] == 2 and runs["c"] == 2
  )  # re-run is CORRECT here
