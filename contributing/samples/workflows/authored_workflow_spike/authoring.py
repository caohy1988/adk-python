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

"""Agent-authored typed Workflows — reference spike (RFC #93).

A minimal, faithful implementation of the RFC's authoring layer:

* ``WorkflowSpec`` — a plain ``kind``-tagged recursive union (a typed plan
  vocabulary; not Pydantic's discriminated union — see the SpecNode note).
* ``CapabilityRegistry`` — the closed set of agents/tools a plan may compose.
* ``WorkflowSpecValidator`` — semantic validation (capability refs, binding
  scope, list/loop/branch rules) + an open-map output-schema warning.
* ``SpecInterpreter`` — executes a validated spec on the real ADK Workflow
  engine via the #92 ``DynamicNodeSupervisor`` (step / fan_out / pipeline /
  branch / loop_until).
* ``FrozenWorkflowRecord`` / ``export_plan`` / ``import_plan`` — the frozen spec
  as a first-class, portable artifact (DESIGN.md §10): export to a JSON
  envelope; import recomputes the hash and re-validates against the *current*
  registry, never trusting the envelope's own ``validation``.

This is a demand-gate artifact, not production code. See README.md.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from typing import Any
from typing import Literal
from typing import Optional
from typing import Union

from pydantic import BaseModel
from pydantic import Field
from pydantic import model_validator

# The #92 supervisor lives in a sibling sample dir.
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..",
        "dynamic_supervisor_spike",
    ),
)
from supervisor import DynamicNodeSupervisor  # noqa: E402


# ----------------------------------------------------------------- WorkflowSpec
class Binding(BaseModel):
  """The only way a node sources input: a source + optional dotted path."""

  source: Literal["task", "step"]
  step: Optional[str] = None
  path: Optional[str] = None

  @model_validator(mode="after")
  def _invariant(self):
    if (self.source == "step") != (self.step is not None):
      raise ValueError("source=='step' iff `step` is set")
    return self


class StepRef(BaseModel):
  kind: Literal["step"]
  id: str
  capability: str
  input: Binding


class FanOut(BaseModel):
  kind: Literal["fan_out"]
  id: str
  over: Binding
  capability: str
  collect: Literal["list"] = "list"


class PipelineStage(BaseModel):
  capability: str  # registered; takes an item
  input: Binding | None = (
      None  # defaults to the previous stage's per-item output
  )


class Pipeline(BaseModel):
  # Barrier-free per-item multi-stage flow: each item runs through ALL stages
  # via #92 ctx.pipeline (item A can be in stage k while item B is in stage 1) —
  # NOT two barriered fan_outs. Compiles to DynamicNodeSupervisor.pipeline.
  kind: Literal["pipeline"]
  id: str
  over: Binding  # MUST resolve to a list
  stages: list[PipelineStage]
  collect: Literal["list"] = "list"


class Route(BaseModel):
  value: str
  block: list["SpecNode"]


class Branch(BaseModel):
  kind: Literal["branch"]
  id: str
  on: Binding
  # Enumerated LIST of routes, NOT an open dict[str, ...] map: open maps are a
  # structured-output reliability hazard (the model leaves them empty). The
  # spike's branch shape exposed exactly this — see README.
  routes: list[Route]
  unmatched: Literal["fail"] = "fail"


class LoopUntil(BaseModel):
  kind: Literal["loop_until"]
  id: str
  body: list["SpecNode"]
  until_capability: str
  until_input: Binding
  max_iters: int = Field(ge=1)


# NOTE: a PLAIN union, not Pydantic's Field(discriminator="kind"). The discriminated
# form emits a JSON schema with a `discriminator` keyword that genai's response_schema
# rejects (Schema: extra_forbidden — verified on gemini-3.5-flash). Each member still
# carries a `kind` Literal, so this is a structurally-tagged union: unambiguous to parse
# and to switch on, AND accepted as a Gemini response_schema.
SpecNode = Union[StepRef, FanOut, Pipeline, Branch, LoopUntil]


class WorkflowSpec(BaseModel):
  goal: str
  steps: list[SpecNode]
  output: Binding


for _m in (
    StepRef,
    FanOut,
    PipelineStage,
    Pipeline,
    Branch,
    Route,
    LoopUntil,
    WorkflowSpec,
):
  _m.model_rebuild()


# ----------------------------------------------------------------- registry
class Capability(BaseModel):
  """A registered capability the planner may compose by name."""

  model_config = {"arbitrary_types_allowed": True}

  name: str
  build: Any  # () -> NodeLike  (an ADK Agent, or a deterministic @node fn)
  input_kind: Literal["item", "list"]
  output_model: Optional[type[BaseModel]] = None
  serialize_input: bool = (
      True  # json.dumps the node_input (True for LLM agents)
  )
  max_fan_out: int = 100
  side_effect: bool = False
  version: str = "1"  # bumped when the capability's contract changes (drift)


class CapabilityRegistry:

  def __init__(self, capabilities: list[Capability], *, version: str = "1"):
    self._by_name = {c.name: c for c in capabilities}
    self.version = version  # registry_version (coarse drift signal)

  def __contains__(self, name):
    return name in self._by_name

  def __getitem__(self, name):
    return self._by_name[name]

  def names(self) -> list[str]:
    return list(self._by_name)

  def capability_versions(
      self, only: Optional[set[str]] = None
  ) -> dict[str, str]:
    """name -> version for drift detection on import (optionally filtered)."""
    return {
        n: c.version
        for n, c in self._by_name.items()
        if only is None or n in only
    }

  def open_map_warnings(self) -> list[str]:
    """Spike lesson: open-ended dict[str, X] output fields are a structured-
    output reliability hazard (Gemini fills them unreliably). Warn on them."""
    warnings = []
    for cap in self._by_name.values():
      model = cap.output_model
      if model is None:
        continue
      for fname, field in model.model_fields.items():
        ann = str(field.annotation)
        if "dict[" in ann.replace(" ", "") and "int]" not in ann.lower()[:0]:
          if ann.replace(" ", "").startswith(
              "dict[str,"
          ) or "dict[str," in ann.replace(" ", ""):
            warnings.append(
                f"capability '{cap.name}': output field '{fname}' is an open"
                f" map ({ann}); prefer enumerated fields for reliable"
                " structured output"
            )
    return warnings


# ----------------------------------------------------------------- validator
class SpecValidationError(Exception):
  pass


class WorkflowSpecValidator:

  def __init__(self, registry: CapabilityRegistry):
    self.registry = registry

  def validate(self, spec: WorkflowSpec) -> list[str]:
    """Raises SpecValidationError on a hard error; returns soft warnings."""
    ids: set[str] = set()
    self._walk(spec.steps, set(), ids)
    if spec.output.source == "step" and spec.output.step not in ids:
      raise SpecValidationError(
          f"output references unknown step {spec.output.step!r}"
      )
    return self.registry.open_map_warnings()

  def _walk(self, nodes, preceding: set[str], ids: set[str]) -> set[str]:
    preceding = set(preceding)
    for n in nodes:
      if n.id in ids:
        raise SpecValidationError(f"duplicate id {n.id!r}")
      ids.add(n.id)
      if isinstance(n, (StepRef, FanOut)) and n.capability not in self.registry:
        raise SpecValidationError(f"unknown capability {n.capability!r}")
      if isinstance(n, LoopUntil) and n.until_capability not in self.registry:
        raise SpecValidationError(
            f"unknown until_capability {n.until_capability!r}"
        )
      # Entry bindings (input/over/on) reference a PRIOR step on this path.
      for f in ("input", "over", "on"):
        b = getattr(n, f, None)
        if (
            isinstance(b, Binding)
            and b.source == "step"
            and b.step not in preceding
        ):
          raise SpecValidationError(
              f"{n.id}: binding references non-preceding step {b.step!r}"
          )
      if (
          isinstance(n, FanOut)
          and self.registry[n.capability].input_kind != "item"
      ):
        raise SpecValidationError(
            f"fan_out {n.id}: capability must take an item"
        )
      if isinstance(n, Pipeline):
        if not n.stages:
          raise SpecValidationError(f"pipeline {n.id}: needs >= 1 stage")
        for st in n.stages:
          if st.capability not in self.registry:
            raise SpecValidationError(f"unknown capability {st.capability!r}")
          if self.registry[st.capability].input_kind != "item":
            raise SpecValidationError(
                f"pipeline {n.id}: stage {st.capability!r} must take an item"
            )
          if (
              isinstance(st.input, Binding)
              and st.input.source == "step"
              and st.input.step not in preceding
          ):
            raise SpecValidationError(
                f"pipeline {n.id}: stage input references non-preceding step"
                f" {st.input.step!r}"
            )
      if isinstance(n, LoopUntil):
        # body executes in-scope; until_input may reference a body step.
        body_scope = self._walk(n.body, preceding | {n.id}, ids)
        ui = n.until_input
        if ui.source == "step" and ui.step not in body_scope:
          raise SpecValidationError(
              f"loop {n.id}: until_input references step {ui.step!r} not in its"
              " body/scope"
          )
      if isinstance(n, Branch):
        for route in n.routes:
          self._walk(route.block, preceding | {n.id}, ids)
      preceding.add(n.id)
    return preceding


def _bindings(n) -> list[Binding]:
  out = []
  for f in ("input", "over", "on", "until_input"):
    b = getattr(n, f, None)
    if isinstance(b, Binding):
      out.append(b)
  return out


# ----------------------------------------------------------- export / import
#
# DESIGN.md §10: the frozen spec is a first-class, exportable artifact. The
# source of truth is the typed WorkflowSpec; the compiled Workflow is derived
# and never stored. A single canonical hash definition keeps two exporters in
# agreement, and import NEVER trusts the envelope's own `validation` — it
# recomputes the hash and re-validates against the *current* registry.


def canonical_json(value) -> str:
  """The one fixed serialization for hashing (no whitespace/key-order drift)."""
  return json.dumps(value, sort_keys=True, separators=(",", ":"))


def sha256_hex(value) -> str:
  return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def referenced_capabilities(spec: WorkflowSpec) -> set[str]:
  """Every capability name a spec composes (walks pipeline stages, branch
  routes, and loop bodies — not just top-level steps)."""
  found: set[str] = set()

  def walk(nodes):
    for n in nodes:
      cap = getattr(n, "capability", None)
      if cap:
        found.add(cap)
      for st in getattr(n, "stages", None) or []:
        found.add(st.capability)
      for route in getattr(n, "routes", None) or []:
        walk(route.block)
      if getattr(n, "until_capability", None):
        found.add(n.until_capability)
      if getattr(n, "body", None):
        walk(n.body)

  walk(spec.steps)
  return found


class ValidationResult(BaseModel):
  passed: bool
  warnings: list[str] = Field(default_factory=list)


class FrozenWorkflowRecord(BaseModel):
  """The single shape behind session state, the audit event, and the export
  envelope (DESIGN.md §5) — v1 storage is never a weaker subset."""

  schema_version: str = "v1"
  spec: WorkflowSpec
  spec_hash: str
  planner_model: str
  registry_version: str
  capability_versions: dict[str, str]
  validation: ValidationResult
  created_at: str  # ISO-8601, stamped at freeze (caller supplies; not now())
  task_input_schema: Optional[dict] = None
  task_input_digest: Optional[str] = None

  @classmethod
  def freeze(
      cls,
      spec: WorkflowSpec,
      *,
      planner_model: str,
      registry: CapabilityRegistry,
      created_at: str,
      task_input=None,
      task_input_schema: Optional[dict] = None,
  ) -> "FrozenWorkflowRecord":
    """Validate + capture everything needed for replay and drift detection."""
    warnings = WorkflowSpecValidator(registry).validate(spec)  # raises on hard
    refs = referenced_capabilities(spec)
    return cls(
        spec=spec,
        spec_hash=sha256_hex(spec.model_dump(mode="json")),
        planner_model=planner_model,
        registry_version=registry.version,
        capability_versions=registry.capability_versions(only=refs),
        validation=ValidationResult(passed=True, warnings=warnings),
        created_at=created_at,
        task_input_schema=task_input_schema,
        task_input_digest=(
            None if task_input is None else sha256_hex(task_input)
        ),
    )


class PlanImportError(Exception):
  """Raised when an exported plan fails integrity, drift, or input checks."""


def export_plan(record: FrozenWorkflowRecord) -> dict:
  """Serialize the §5 record to a portable JSON-able envelope."""
  return record.model_dump(mode="json")


def import_plan(
    envelope: dict, registry: CapabilityRegistry, *, task_input=None
) -> WorkflowSpec:
  """Re-hydrate an exported plan, NEVER trusting the envelope's own checks.

  Integrity + drift (DESIGN.md §10):
    1. recompute sha256(canonical_json(spec)); REJECT if != envelope spec_hash;
    2. re-run WorkflowSpecValidator against the CURRENT registry (catches a
       dropped/renamed capability);
    3. per-capability version drift vs the envelope -> fail loudly.
  Execution-input contract:
    * replay   (no schema): task_input digest MUST match the envelope's;
    * template (schema):    task_input is validated against task_input_schema;
    * neither: do NOT execute against arbitrary new input.
  """
  spec = WorkflowSpec.model_validate(envelope["spec"])

  # 1. integrity — recompute, don't trust.
  recomputed = sha256_hex(spec.model_dump(mode="json"))
  if recomputed != envelope.get("spec_hash"):
    raise PlanImportError(
        "spec_hash mismatch: envelope has"
        f" {envelope.get('spec_hash')!r}, recomputed {recomputed!r} — the spec"
        " was tampered with or re-serialized under a different definition"
    )

  # 2. re-validate against the CURRENT registry (dropped capability fails here).
  try:
    WorkflowSpecValidator(registry).validate(spec)
  except SpecValidationError as e:
    raise PlanImportError(f"re-validation against current registry failed: {e}")

  # 3. per-capability version drift.
  current = registry.capability_versions(only=referenced_capabilities(spec))
  recorded = envelope.get("capability_versions", {})
  drifted = {
      n: (recorded.get(n), current.get(n))
      for n in current
      if recorded.get(n) != current.get(n)
  }
  if drifted:
    raise PlanImportError(
        f"capability version drift (recorded vs current): {drifted} — promote"
        " to a template with explicit migration before reuse"
    )

  # Execution-input contract.
  if task_input is not None:
    schema = envelope.get("task_input_schema")
    if schema is not None:
      missing = [k for k in schema.get("required", []) if k not in task_input]
      if missing:
        raise PlanImportError(
            f"task input missing required keys {missing} for this template"
        )
    else:
      digest = sha256_hex(task_input)
      if digest != envelope.get("task_input_digest"):
        raise PlanImportError(
            "task_input digest mismatch and no task_input_schema captured:"
            " this plan can only be REPLAYED on its original input (promote to"
            " a template to reuse it on new input)"
        )

  return spec


# ----------------------------------------------------------------- interpreter
class SpecInterpreter:
  """Executes a validated WorkflowSpec on the real ADK engine via the #92
  supervisor. Handles step / fan_out / pipeline / branch / loop_until."""

  def __init__(self, registry: CapabilityRegistry, ctx, *, gate: int = 8):
    self.registry = registry
    self.ctx = ctx
    self.sup = DynamicNodeSupervisor(ctx, gate=gate)
    self.state: dict[str, Any] = {}

  def _resolve(self, binding: Binding, task_input):
    base = task_input if binding.source == "task" else self.state[binding.step]
    if binding.path:
      cur = base
      for part in binding.path.split("."):
        cur = cur[part] if isinstance(cur, dict) else getattr(cur, part)
      return cur
    return base

  def _arg(self, cap: Capability, value):
    return json.dumps(value, default=str) if cap.serialize_input else value

  async def _dispatch(self, cap_name: str, value, run_id: str):
    cap = self.registry[cap_name]
    return await self.sup.dispatch(
        cap.build(), node_input=self._arg(cap, value), run_id=run_id
    )

  async def execute(self, spec: WorkflowSpec, task_input) -> Any:
    await self._run_block(spec.steps, task_input, prefix="")
    return self._resolve(spec.output, task_input)

  async def _run_block(self, nodes, task_input, prefix: str):
    last = None
    for n in nodes:
      rid = f"{prefix}{n.id}"
      if isinstance(n, StepRef):
        self.state[n.id] = await self._dispatch(
            n.capability, self._resolve(n.input, task_input), rid
        )
      elif isinstance(n, FanOut):
        cap = self.registry[n.capability]
        items = self._resolve(n.over, task_input)
        if len(items) > cap.max_fan_out:
          raise SpecValidationError(
              f"runtime: fan_out {len(items)} exceeds max_fan_out"
              f" {cap.max_fan_out}"
          )
        self.state[n.id] = await self.sup.pipeline(
            items,
            (
                lambda _p, it, i, c=cap, rid=rid: self.sup.dispatch(
                    c.build(), node_input=self._arg(c, it), run_id=f"{rid}_{i}"
                )
            ),
        )
      elif isinstance(n, Pipeline):
        # Barrier-free per-item multi-stage flow via #92 ctx.pipeline — each item
        # threads ALL stages; item A can be in stage k while item B is in stage 1
        # (NOT two barriered fan_outs). stage[0] input defaults to the per-item
        # element; stage[k] input defaults to stage[k-1]'s per-item output.
        items = self._resolve(n.over, task_input)
        # Each stage dispatches once per item, so every stage capability is
        # subject to the same data-dependent fan-out cap as a FanOut.
        for st in n.stages:
          cap = self.registry[st.capability]
          if len(items) > cap.max_fan_out:
            raise SpecValidationError(
                f"runtime: pipeline stage {st.capability!r} fan_out"
                f" {len(items)} exceeds max_fan_out {cap.max_fan_out}"
            )
        stage_fns = []
        for si, st in enumerate(n.stages):

          def stage(prev, it, i, si=si, st=st, rid=rid):
            cap = self.registry[st.capability]
            value = (
                self._resolve(st.input, task_input)
                if st.input is not None
                else (it if si == 0 else prev)
            )
            return self.sup.dispatch(
                cap.build(),
                node_input=self._arg(cap, value),
                run_id=f"{rid}_{i}_{si}",
            )

          stage_fns.append(stage)
        self.state[n.id] = await self.sup.pipeline(items, *stage_fns)
      elif isinstance(n, Branch):
        value = str(self._resolve(n.on, task_input))
        routes = {r.value: r.block for r in n.routes}
        if value not in routes:
          raise SpecValidationError(
              f"runtime: branch {n.id} unmatched value {value!r}"
              " (unmatched=fail)"
          )
        out = await self._run_block(
            routes[value], task_input, prefix=f"{rid}_{value}_"
        )
        self.state[n.id] = out
      elif isinstance(n, LoopUntil):
        out = None
        for i in range(n.max_iters):
          out = await self._run_block(n.body, task_input, prefix=f"{rid}_i{i}_")
          verdict = await self._dispatch(
              n.until_capability,
              self._resolve(n.until_input, task_input),
              f"{rid}_i{i}_until",
          )
          if _truthy(verdict):
            break
        self.state[n.id] = out
      last = self.state.get(n.id)
    return last


def _truthy(v) -> bool:
  if isinstance(v, bool):
    return v
  if isinstance(v, dict):
    for k in ("result", "value", "done", "ok"):
      if k in v:
        return bool(v[k])
  return bool(v)
