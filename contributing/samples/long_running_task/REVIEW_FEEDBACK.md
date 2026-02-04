# Durable Session Persistence for Long-Horizon Data Research Agents

**Authors:** Haiyuan Cao
**Date:** 02/03/2026

---

## Objective

To enable a new class of **Long-Horizon Deep Research Agents** in BigQuery that can execute multi-day autonomous investigations. By implementing a **Durable Session Persistence Layer**, these agents move beyond simple Q&A to perform complex, asynchronous rollouts—cross-dataset synthesis, persistent document monitoring, and multi-step "deep dives"—that survive cloud sandbox timeouts and process restarts.

---

## 1. Current State & Limitations

It is critical to distinguish between ADK's existing *session delegation* capabilities and the proposed *durable execution* model.

### 1.1 Existing Capability: `ResumabilityConfig`

ADK currently supports an experimental `ResumabilityConfig` (see `src/google/adk/apps/app.py`) that allows an agent to "pause" execution when calling a long-running tool.

* **Mechanism:** The invocation pauses in-memory. If the user polls the API or the tool returns, the specific runner instance resumes the thread.
* **Storage Delegation:** ADK can currently delegate session storage (chat history) to BigQuery or Postgres. This saves the *conversation log* (`User: ...`, `Agent: ...`).

### 1.2 The Gap: Chat History vs. Execution State

While ADK can save *what was said* (Chat History) to BigQuery, it does not currently save *what the agent is thinking* (Execution State) in a way that survives a crash.

| Feature | Current ADK (BQ Session Delegation) | Proposed Durable Extension |
| --- | --- | --- |
| **What is Saved?** | **Conversation Log:** Messages, user inputs, final tool outputs. | **Execution Snapshot:** The "Job Ledger," partial plans, active stack frames, and retry counters. |
| **Process Death** | **Fatal:** If the runner crashes, the "thought process" is lost. You can reload chat history, but the agent forgets it was waiting for Job ID #504. | **Recoverable:** The agent wakes up, reads the checkpoint, and knows exactly where it left off in the logic loop. |
| **Resume Trigger** | **Reactive:** Requires a user or API to poke the agent. | **Proactive:** Can wake itself up via Pub/Sub events (e.g., "Job Done"). |
| **Consistency** | **Event Replay:** Replays history to rebuild context (expensive & fragile). | **Authoritative Reconciliation:** Deterministically syncs with cloud state (BQ Information Schema). |

**Problem Statement:** Current workflows are brittle. If a Cloud Run instance recycles during a 4-hour job, the agent effectively "dies," leaving orphaned BigQuery jobs and no record of its intent. We need a way to hibernate the *brain*, not just the *transcript*.

---

## 2. The Solution: Two-Phase Commit Persistence

We introduce a **Two-Phase Commit** mechanism to ensure every research step is durably persisted before the agent hibernates. This extends `ResumabilityConfig` to support cross-process durability.

### 2.1 Architecture

1. **Phase 1 (GCS - Data Plane):** The agent serializes its "Research Notebook" (partial drafts, URL ledgers, and reasoning state) to GCS. This handles the bulk state (>1MB).
2. **Phase 2 (BigQuery - Control Plane):** A metadata row is inserted into a new `checkpoints` table. **Crucially, a checkpoint is only considered "live" once this row commits.** This ensures atomic visibility and prevents "half-saved" states.
3. **Hibernation:** The agent releases all compute resources. Cost drops to near zero.

### 2.2 Authoritative Reconciliation

Upon waking (via Pub/Sub), the agent does not blindly trust the event stream. It performs a **Deterministic Sync**:

* It loads the checkpoint from GCS.
* It queries `INFORMATION_SCHEMA.JOBS` to verify the actual status of delegated tasks.
* It updates its internal ledger (e.g., marking "RUNNING" tasks as "FAILED" if BQ reports a quota error) before generating the next step.

---

## 3. CUJ: Autonomous Deep Research & Trend Analysis

**Persona:** Strategic Market Analyst
**User Story:** *"As an analyst, I want to trigger a deep research mission. I want the agent to scan three years of financial filings, monitor news for 48 hours, and synthesize a strategy memo without me keeping a tab open."*

### Agent-Based Journey:

1. **Initiation:** A Cloud Scheduler triggers the agent.
* **Mission:** "Analyze `global_financials` for R&D trends. Monitor `live_news_stream` for 24h."


2. **Execution & Hibernation:** The agent submits 30 complex BigQuery jobs. It performs a **Two-Phase Commit** to save its state to GCS/BQ, then enters **PAUSED**.
3. **Event-Driven Resume:** A Pub/Sub message (BQ Job Complete) triggers the **Resume Service**.
4. **Reconciliation:** The agent acquires a **Lease** (preventing race conditions), loads the checkpoint, and reconciles its ledger. It finds 28 successes and 2 failures, schedules retries, and drafts the report.
5. **Outcome:** 24 hours later, a final Markdown report is written to the `executive_briefings` table.

---

## 4. System Design & Schemas

### 4.1 BigQuery Schema (Control Plane)

*Additions based on Technical Review:*

```sql
-- Checkpoints Table (Control Plane)
CREATE TABLE `adk_metadata.checkpoints` (
  session_id STRING NOT NULL,
  checkpoint_seq INT64 NOT NULL,
  gcs_state_uri STRING NOT NULL,
  sha256 STRING NOT NULL,  -- Integrity check
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP(),
  agent_state_summary JSON, -- For querying "what was the agent thinking?"
  PRIMARY KEY (session_id, checkpoint_seq) NOT ENFORCED
);

-- Events Table (Trigger Log)
CREATE TABLE `adk_metadata.events` (
  event_id STRING NOT NULL,
  session_id STRING NOT NULL,
  event_type STRING, -- e.g., 'BQ_JOB_DONE', 'PUB_SUB_MSG'
  payload JSON,
  processed BOOL DEFAULT FALSE,
  PRIMARY KEY (event_id) NOT ENFORCED
);

```

### 4.2 Cost Estimation

By shifting from "Idle Compute" to "Durable Pause":

* **Compute:** Drops from ~$1.40/day (Cloud Run idle) to **$0**.
* **Storage:** BQ Rows + GCS Blobs cost **<$0.01/day** per active session.
* **Total Savings:** >99% reduction for long-horizon tasks.

### 4.3 Rollback & Safety

* **Leasing:** We use a BQ `active_lease_id` column with a 5-minute TTL to ensure only one runner owns a session.
* **Rollback:** If a runner crashes *during* a checkpoint write, the Phase 2 (BQ commit) never happens. The system simply resumes from the *previous* valid checkpoint, ensuring zero corruption.

---

## Q&A

**Why can't we just use the existing BQ Session Storage?**
Existing BQ storage saves the *transcript*. If an agent has processed 500 documents and built a complex internal mental model, reloading the transcript and asking the LLM to "re-read" everything to rebuild that mental model is slow, expensive, and error-prone. This proposal saves the *mental model itself*.

**What happens if the Resume Service fails?**
The architecture is idempotent. Pub/Sub will redeliver the message. The Resume Service will check the BQ `events` table; if the event is already marked `processed`, it ignores it. If not, it acquires the lease and proceeds.

**Is this only for BigQuery?**
No. While BigQuery is the control plane, this pattern works for any long-running async task (e.g., waiting for human approval, video rendering, or external API webhooks).
