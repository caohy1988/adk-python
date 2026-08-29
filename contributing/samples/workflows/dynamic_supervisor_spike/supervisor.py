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

"""Prototype DynamicNodeSupervisor for concurrent dynamic dispatch.

This is the RFC spike artifact (see README.md). It layers concurrent
``ctx.run_node()`` orchestration on the real ADK Workflow engine via a
framework-owned supervisor. Two design decisions are encoded here and
verified by the tests:

1. The concurrency gate is acquired around each LEAF dispatch (a single
   ``ctx.run_node`` call), NOT around drivers. Orchestration frames
   (drivers, fan-out, nested pipeline/parallel) hold no permit while
   awaiting children, so nesting a combinator inside a stage cannot
   deadlock. (Gating drivers DOES deadlock — see the contrast test.)

2. Fan-out uses ``asyncio.TaskGroup`` (structured concurrency), NOT
   ``asyncio.gather``. ``gather`` propagates an exception but does not
   cancel siblings.

Failure / cancellation contract (verified by the tests):

* Ordinary ``Exception`` in a branch -> that branch becomes ``None``;
  siblings are unaffected.
* ``NodeInterruptedError`` (and other non-cancellation ``BaseException``
  such as ``KeyboardInterrupt`` / ``SystemExit``) -> propagates, and
  ``TaskGroup`` cancels the remaining branches.
* External cancellation of the combinator -> propagates down to the
  in-flight branches (standard structured concurrency).
* A branch raising ``asyncio.CancelledError`` itself is treated by asyncio
  as that task's own cancellation: ``TaskGroup`` does NOT propagate it and
  does NOT cancel siblings; the branch's slot is left ``None`` and the
  others run to completion. (This is asyncio semantics, not something the
  supervisor can override without bespoke handling — see the test.)
"""

from __future__ import annotations

import asyncio
import os
from typing import Any
from typing import Awaitable
from typing import Callable
from typing import Sequence

from google.adk.workflow._errors import NodeInterruptedError

# Control exceptions are NEVER converted to None. NodeInterruptedError is a
# BaseException by design so it cannot be swallowed by ``except Exception``.
_CONTROL_EXC = (
    NodeInterruptedError,
    asyncio.CancelledError,
    KeyboardInterrupt,
    SystemExit,
)


def default_gate() -> int:
  """min(16, cpu-2): matches the Claude Code reference runtime cap."""
  return min(16, max(1, (os.cpu_count() or 3) - 2))


class DynamicNodeSupervisor:
  """Drives concurrent dynamic ``ctx.run_node()`` chains under one parent."""

  def __init__(self, ctx, *, gate: int | None = None) -> None:
    self.ctx = ctx
    resolved_gate = gate if gate is not None else default_gate()
    if resolved_gate < 1:
      raise ValueError(f"gate must be >= 1, got {resolved_gate}")
    self.gate = asyncio.Semaphore(resolved_gate)
    self.peak_in_flight = 0
    self._in_flight = 0

  async def dispatch(
      self, child, *, node_input: Any = None, run_id: str | None = None
  ) -> Any:
    """One leaf dispatch. The gate is held ONLY for the child execution.

    Each dispatch runs in its OWN sub-branch AND its own isolation scope
    (parent_scope::run_id). Parallel siblings share an author name, and a
    single_turn LLM child making MULTIPLE model calls (tool loops) rebuilds
    its context per call by scanning for the latest input event in its
    isolation scope — with the parent's shared scope, sibling inputs landing
    in between get picked up instead (observed: fanned-out tool-using
    skeptics all answering the LAST sibling's claim). The wrapper stamps the
    child's input event and its FC/FR trail with ctx.isolation_scope, so a
    per-dispatch scope makes context independence structural for multi-call
    children rather than an artifact of single-call timing.
    """
    async with self.gate:
      self._in_flight += 1
      self.peak_in_flight = max(self.peak_in_flight, self._in_flight)
      parent_scope = getattr(self.ctx, "isolation_scope", None)
      scope = f"{parent_scope}::{run_id}" if run_id else parent_scope
      try:
        return await self.ctx.run_node(
            child,
            node_input=node_input,
            run_id=run_id,
            use_sub_branch=True,
            override_isolation_scope=scope,
        )
      finally:
        self._in_flight -= 1

  async def _guard_ordinary(self, factory: Callable[[], Awaitable[Any]]) -> Any:
    """Ordinary Exception -> None (drop the branch). Control exceptions escape."""
    try:
      return await factory()
    except _CONTROL_EXC:
      raise
    except Exception:  # noqa: BLE001 - includes DynamicNodeFailError
      return None

  async def _supervise(
      self, factories: Sequence[Callable[[], Awaitable[Any]]]
  ) -> list[Any]:
    """Structured fan-out via TaskGroup. See the failure/cancellation contract
    in the module docstring: ordinary failure -> None; NodeInterruptedError
    (and other non-cancellation BaseException) propagates and cancels the rest;
    a branch's own CancelledError leaves its slot None without cancelling
    siblings. Results preserve input order.
    """
    results: list[Any] = [None] * len(factories)

    async def _run_one(i: int, f: Callable[[], Awaitable[Any]]) -> None:
      results[i] = await self._guard_ordinary(f)

    try:
      async with asyncio.TaskGroup() as tg:
        for i, f in enumerate(factories):
          tg.create_task(_run_one(i, f))
    except* NodeInterruptedError:
      raise NodeInterruptedError()
    return results

  async def parallel(
      self, thunks: Sequence[Callable[[], Awaitable[Any]]]
  ) -> list[Any]:
    """BARRIER fan-out. thunks: zero-arg callables returning awaitables."""
    return await self._supervise(thunks)

  async def pipeline(
      self,
      items: Sequence[Any],
      *stages: Callable[[Any, Any, int], Awaitable[Any]],
      gate_drivers: bool = False,
  ) -> list[Any]:
    """Barrier-free per-item pipelining. Stage signature: (prev, item, index).

    Each item flows through all stages independently; item A may be in stage
    k while item B is in stage 1. An ordinary Exception in a stage drops that
    item to None; control exceptions propagate.

    ``gate_drivers=True`` is the intentionally-BUGGY variant used by the
    contrast test to demonstrate the nested-combinator deadlock.
    """

    def make_driver(item: Any, i: int) -> Callable[[], Awaitable[Any]]:
      async def drive() -> Any:
        prev = item
        for stage in stages:
          prev = await stage(prev, item, i)
        return prev

      if gate_drivers:

        async def gated() -> Any:
          async with self.gate:  # gating the DRIVER -> deadlock on nesting
            return await drive()

        return gated
      return drive

    return await self._supervise(
        [make_driver(it, i) for i, it in enumerate(items)]
    )
