# ADK Web demo — model-authored workflows for BigQuery Conversational Analytics (RFC #93)

One agent, **seven prompts, seven workflow shapes**. Styled after [BigQuery
Conversational Analytics](https://docs.cloud.google.com/bigquery/docs/conversational-analytics):
a user asks data questions in natural language, and the planner **authors a
different typed `WorkflowSpec` per scenario** over Conversational-Analytics
capabilities — `nl2sql`, `dry_run`, `run_query`, `profile_table`, `skeptic`,
chart judging — against a mock `thelook_ecommerce` dataset (the dataset the
CA docs demo against). **Query execution is REAL BigQuery** when
credentials allow: `dry_run` hits the actual BigQuery dry-run API (real
errors, real bytes-scanned) and `run_query` executes against
`bigquery-public-data.thelook_ecommerce`, billed to your
`GOOGLE_CLOUD_PROJECT` with safety rails (`maximum_bytes_billed` = 2 GB per
query, 500-row result cap). Multi-dimensional questions ("each region's
trend per year") return real grouped results and chart as multi-series
lines. Without credentials (or with `CA_DEMO_USE_BIGQUERY=0`), execution
falls back to a deterministic micro-warehouse (synthetic facts +
SQL-intent aggregation) so CI and credential-less machines keep working —
each dry-run/result beat carries an `engine` field (`bigquery` or `mock`)
so the demo never misrepresents its data source. The language steps
(NL2SQL, summaries, classification, skeptics) are live Gemini calls.

Every scenario runs the full #93 machinery: **author → validate →
independence lints → freeze (per-scenario key) → execute on the real engine
(#92 supervisor) → cost line**, and every shape is pinned in CI with the
language capabilities stubbed.

## 0. Configure a model (no hardcoded project)

```bash
export GOOGLE_GENAI_USE_VERTEXAI=1
export GOOGLE_CLOUD_PROJECT=<your-project>
export GOOGLE_CLOUD_LOCATION=global
export SPIKE_GEMINI_MODEL=gemini-3.5-flash
```

## 1. Run it

```bash
adk web contributing/samples/workflows/authored_workflow_ca_demo --port 8001
```

**Talk to it first** — the agent has a conversational gate (the RFC's
"no-plan escape hatch"): untriggered messages are intent-classified, and
meta/chit-chat turns get a direct answer instead of a workflow. Try:

```text
What kinds of workflow can you issue?
```

→ a plain-language catalogue of the seven shapes with example prompts — `0 planner calls, 0 queries`. Data questions proceed to the machinery below.

Open the UI, pick `bq_ca_planner`, and send the prompts below — **one
scenario per prompt**, each authoring a different coordination shape:

| #   | Send this prompt                                             | Shape authored                                                                     | CA story                                                                                                                                                    |
| --- | ------------------------------------------------------------ | ---------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | `What was revenue by region last quarter?`                   | `loop_until(draft → REAL dry-run → repair) → run_query → render_chart + summarize` | the standard CA flow — **your actual question is the task input**, and a real BigQuery dry-run error (e.g. `TIMESTAMP_SUB ... YEAR`) feeds the repair round |
| 2   | `Profile data quality across the dataset tables.`            | fan-out → synthesize                                                               | per-table profiling in parallel, one report                                                                                                                 |
| 3   | `Build a dashboard for these three questions.`               | pipeline(`nl2sql → dry_run`) per item                                              | each panel translated + validated barrier-free                                                                                                              |
| 4   | `Route my question: what does order status 'Complete' mean?` | classify & route (branch)                                                          | metadata questions skip SQL entirely                                                                                                                        |
| 5   | `Answer with SQL self-repair — the dry run is unreliable.`   | loop_until + **loop-carried `init`**                                               | draft → failed dry run → repair using the error                                                                                                             |
| 6   | `Audit these insights — verify each one independently.`      | adversarial verification                                                           | independent skeptics per insight; the $1M AOV claim gets refuted                                                                                            |
| 7   | `Pick the best chart for revenue by region.`                 | tournament                                                                         | pairwise chart judging to a single winner                                                                                                                   |

What to point at as each one streams:

- **🗂️ scenario banner** — the expected shape, named before the model authors it.
- **📋 authored plan** — a *different* typed `WorkflowSpec` per prompt; same closed vocabulary every time.
- **✅ + 🧪 validation & independence lints** — every scenario lints clean; the provenance facts are statically provable from the bindings.
- **🔒 freeze (per-scenario key) + 📦 cross-session export** — every authored plan exports its full `FrozenWorkflowRecord` to `ca_plan_store/<scenario>.json`. **Re-send any prompt**: same hash, `0 planner calls (frozen replay)`. **Start a whole new session** and ask again: the plan is **imported from the store** through the RFC's defensive path — spec hash recomputed, re-validated against the current registry, manual-version + declared-contract drift fail loudly (input kind + declared output schema; the typed object-output capabilities declare output models so the hash has teeth — primitive helpers like `sql_ok`/`judge_chart` return bare bool/str/list values and rely on manual versions) (with the rejection shown, then a fresh authoring), and your new question is validated against the captured `task_input_schema` (cross-session **template reuse**). Plans now outlive sessions.
- **template reuse (scenario 1)** — after the first ask, send a *different* question (`What was revenue by region last year?`): the frozen plan is reused unchanged, your new question flows through it as new task input, and the mock rows change with the window (quarter vs year canned sets). Same plan, new data — the RFC's replay-vs-template distinction, live.
- **📈 chart** — scenarios 1 and 7 emit the Conversational-Analytics-style chart artifact: a **rendered chart image inline in the chat** (matplotlib, optional — falls back to a Unicode preview) plus the **Vega-Lite spec** (what the real CA API returns). Time-series rows infer a line mark; in the tournament, the bracket picks the mark and `render_chart` draws the data with it.
- **honest failure handling** — a query that still fails after repair returns empty rows + the real error (`engine: bigquery`); the mock warehouse is used ONLY when credentials are absent, never to paper over a failing query.
- **📄 result + 📊 cost** — real execution on the #92 supervisor; the repair scenario shows exactly one repair iteration (`Table not found … did you mean orders?` → fixed), the audit scenario rejects the implausible insight, the tournament returns `["bar"]`.

Talking point for scenario 5 (the differentiated one): *the repair loop needs
**loop-carried state** — the drafting step reads the loop's own id to get the
prior round's failed dry-run output. That's `LoopUntil.init`, the vocabulary
gap the pattern-coverage sweep surfaced. And the whole loop is frozen and
replayable — a turn-by-turn agent retry never is.*

## 2. Correctness proof (no LLM, no BigQuery)

```bash
pytest contributing/samples/workflows/authored_workflow_ca_demo/test_ca_demo_agent.py -q   # 32 (one live-gated: CA_DEMO_LIVE_BQ=1)
```

All seven expected shapes are built by hand, validated + lint-checked against
the demo registry, and **executed end-to-end** with the language capabilities
stubbed: the loop repairs exactly once, the branch routes the metadata
question away from SQL, the audit rejects the implausible insight, the
tournament converges to `bar` and renders it as a Vega-Lite chart artifact. The fan-out and tournament scenarios execute
against the **live** registry (their capabilities are deterministic mocks).

## Notes

- Honesty: like the security-audit demo, scenario recipes are
  instruction-guided so each prompt reliably authors its intended shape; the
  free-decomposition evidence is the spike's demand gate and the main demo's
  free-authoring beat. The *variety* — seven shapes from one closed
  vocabulary — is the claim here.
- The `flaky_dry_run` failure is simulated (every odd call fails) so the
  repair loop behaves identically on every run and in CI.
- Frozen plans are per-scenario (`authored_workflow:ca:<scenario>`) in
  session state, AND exported per-scenario to `ca_plan_store/` for
  cross-session reuse (delete a file to force fresh authoring; the store is
  the demo's stand-in for the ArtifactService in the RFC's revised Q1).
- Scenario 1 takes your live message as the question; the other six prompts
  are mode selectors with canned task inputs (their results don't change
  with your wording). Query answers come from real BigQuery when
  credentials allow (check the `engine` field in the dry-run/result beats);
  otherwise the deterministic micro-warehouse.
