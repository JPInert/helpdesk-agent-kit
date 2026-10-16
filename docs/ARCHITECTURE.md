# Architecture

One ticket goes through the stages below. Everything between the model calls is plain code
with tests. All of it is exercised against mock systems; none of it has run against a live
Zendesk or a live model from this repo.

```
ingest ─▶ vision ─▶ classify ─▶ for each issue ──────────────────────────────▶ merge ─▶ review ⇄ revise ─▶ send
 API       model      model     enrich ─▶ analyze/diagnose ─▶ match ─▶ apply     one      model/lint  bounded   html
                               N systems   no model           YAML    guarded   message                       │
                                                                                                         follow-up (timers)
```

## Modules

| Concern | Module | Notes |
|---|---|---|
| Tickets in and out | `zendesk.py`, `ingest.py` | Public API only. `FixtureSource` reads `fixtures/` and records writes. |
| Clean text | `textclean.py` | Invisible and odd Unicode out of names before they are compared. |
| Systems | `systems/` | One adapter per back-office system, a registry, a lazily opened `SystemSet`. |
| Facts | `facts.py` | Everything the systems said, merged, with the system named per value. Disagreements are kept as conflicts. |
| Approvals | `approval.py` | Parses the free-text approval record, compares what the ticket claims with what the systems say. |
| Diagnosis | `diagnose.py` | Seven rules over the merged facts. No model. |
| Analysis | `analyze.py` | Turns enrichment into a state, a confidence, evidence, findings. One result per rejected record for session tickets. |
| Rules | `kb.py`, `kb/rules.yaml` | State plus type plus submitter picks a rule: reply, action, who to copy. |
| Fix safety | `safety.py`, `actions.py`, `db.py` | The choke point, the allowlist, the ledger, per-row skips. |
| Unseen issues | `proposals.py` | Drafts a rule for an issue with no rule and queues it for a person. |
| Replies | `reply.py`, `revise.py`, `send.py`, `followup.py` | Build, review, revise (bounded), send, follow up. |
| Queue | `triage.py` | Harvest, order, job table, seen registry. |
| People | `dashboard.py` | Review dashboard, token header. |
| Numbers | `stats.py` | Aggregate counts and averages only. |

## Units

A ticket can hold several issues (`Classification.issues()`), and a session issue yields one
analysis per rejected record. Each of these is a unit: it gets its own rule, its own fix
decision, its own row in `unit_results`. Only the replies are merged, into one message, and
that merged message is what the reviewer checks.

## Who may change something

`safety.py` is the only place that answers "may the agent write this?".

1. Nothing routed to a person runs, whatever the rule or the model said.
2. The diagnosis must be on the allowlist, with the one action named for it
   (`job_stuck` retries the job, `record_never_ended` closes the session).
3. Without a person's approval the rule must also opt in, confidence must be HIGH, and the rule
   needs 5 confirmed solves.
4. In the database: `attempts` allows one try per ticket per record, and `fix_ledger` allows
   one fix per record, ever, for any ticket. The ledger refuses deletes and rewrites.
5. After the write, the fix is believed only when a re-read of the system shows it.

Refused rows are written to `row_skips` with the reason.

## Adding things

* **A system**: subclass `SystemAdapter` (login, session handling, `collect`), add one
  `register(...)` call in `systems/__init__.py`, set `serves` to the ticket types it is read for.
* **A rule**: edit `kb/rules.yaml`. No code. Or approve one from the proposals queue.
* **A fix**: add an `Action` in `actions.py` and put its diagnosis on `FIX_ALLOWLIST` in
  `safety.py`. Both edits are needed on purpose.
* **A diagnosis**: a function in `diagnose.py` and a test.

## Data

One SQLite file. `events` and `attempts` and `fix_ledger` are append-only. The rest is current
state: tickets, classifications, enrichments, findings, per-ticket `proposals`, per-unit
`unit_results`, `rule_proposals`, `waiting`, `queue_runs`, `queue_jobs`, `seen_tickets`.
