# helpdesk-agent-kit

An AI agent for an IT help desk queue. It reads Zendesk tickets, including the screenshots
people paste in, works out what the person actually needs, looks them up in several back-office
admin systems that have **no API**, fixes the common cases by driving those systems, and drafts a
reply that a second model checks before anything is sent.

It is built to be trusted with real tickets. The model never has the last word: the
decisions that matter are made by deterministic code, every claim in a reply has to be backed
by data the run actually collected, and a person stays in the loop until a fix has a track
record.

> **Status: in testing.** It works end to end against the included mock systems and demo
> tickets (see [What is proven](#what-is-proven)), and I am still testing and changing it.

## Why I built it

I work in IT, and a lot of the help desk queue I handle was the same handful of problems: a
request that never went through, a record stuck in a queue, an account in a state nobody had
noticed. Each one meant reading the ticket, squinting at a screenshot, looking the person up in
an internal admin portal with no API, fixing the record by hand, and typing the same reply
again.

So I built an agent to do that loop at work, and over a couple of months it went from reading
tickets to fixing them with me approving each change. Most of what I learned was not about
the model. It was about the edges: tickets where the subject says one thing and the body
another, screenshots pasted in a way the API does not report, portals that log you out if you
check whether you are logged in, a status that reads stale for a few seconds after you change
it. This repo is a **clean-room rewrite** of those patterns for anyone to use. No code or data
from the original is in it. The account-provisioning portal, the people and the tickets here
are all invented stand-ins, chosen because every IT team knows them.

## What it does

```
ingest ─▶ vision ─▶ classify ─▶ per issue: enrich ─▶ analyze ─▶ match ─▶ apply ─▶ merge ─▶ review ⇄ revise ─▶ send ─▶ follow-up
 API       model      model       N systems  diagnose   YAML     guarded   one      model/     bounded     html     timers
                                              no model                     reply    lint
```

| Stage | What happens | Uses a model? |
|---|---|---|
| **Ingest** | Pulls the ticket, the requester and the **whole thread** from the Zendesk API, and cleans invisible Unicode out of names. Spots tickets that need the screenshot path, skips monitoring alerts, flags duplicates. | No |
| **Vision** | Turns each screenshot into structured fields (screen, exact error text, username, status). | Yes |
| **Classify** | Ticket type, the product involved, who actually has the problem (a manager often files for someone else), and **every separate issue** in the ticket. | Yes |
| **Enrich** | For each issue, asks every back-office system that serves it, through an **adapter registry**. Each adapter owns its own login and session handling. Results merge into one facts object that remembers which system said each fact. | No |
| **Analyze** | Compares what the ticket says with what the systems show and writes **field-level findings** ("ticket says X, system Y says Z"). For rejected session records it runs **seven deterministic diagnosis rules**. Pure and unit-tested. | No |
| **Match** | Picks a rule from `kb/rules.yaml`: action, confidence, who to copy in, and the reply. An issue with no rule goes to a person **and** files a drafted rule in a proposals queue. | No |
| **Apply** | Runs the fix only if the single choke point `should_auto_execute` allows it, a record is fixed at most once (enforced in the database), then **re-reads until the change actually shows**. | No |
| **Reply** | Fills each issue's reply template from collected evidence only, then merges the replies into one message. | No |
| **Review / revise** | A second model reads the reply against the verified facts: PASS, FLAG or ERROR. A FLAG is revised and re-reviewed, at most twice. | Yes |
| **Send** | Keeps the sign-off together with the name, turns every bare URL into a link, refuses text with placeholder or draft words. Public reply, or an internal note for a person. | No |
| **Follow-up** | Reminds, then closes out, tickets that are waiting on the requester. | No |

### The pieces

| Piece | Where | Highlights |
|---|---|---|
| Ingestion | `helpdesk_kit/ingest.py`, `zendesk.py` | Public Zendesk API only. Detects the three cases the API alone gets wrong: messaging-widget tickets, image attachments, and screenshots pasted into an email (an `<img>` in the HTML with **no** attachment entry). |
| Image parsing | `helpdesk_kit/vision.py` | Structured output from Claude's vision. Strips the model's "no X was visible" sentences, which keyword checks would otherwise read as "X is there", while keeping quoted on-screen text. |
| LLM routing | `classify.py`, `analyze.py`, `diagnose.py`, `kb.py`, `reply.py` | The model classifies; code decides. Most-specific-rule matching, keyword rules for tickets with no record behind them, and a reply reviewer whose failure blocks sending. |
| Back-office systems | `helpdesk_kit/systems/`, `portal.py`, `mock_portal/` | An adapter per system and a registry. Three mock systems ship so you can run it: a provisioning portal driven with Playwright, an approvals system and an activity log, each with its own login and session traps. |
| Fix safety | `safety.py`, `actions.py`, `db.py` | One choke point, an allowlist of fixable diagnoses, a ledger in SQLite, per-row skips with reasons. |
| People | `dashboard.py`, `proposals.py`, `triage.py`, `stats.py` | Review dashboard, rule proposals queue, queue triage with a resumable job table and a seen-ticket registry, aggregate stats. |

A short map of how these fit together is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Design rules it enforces in code

Each of these is a mistake an automation like this makes in real life, and each has a test.

| Rule | How |
|---|---|
| Never claim a fix the run did not confirm | The "it's fixed" reply needs `${new_status}`, which only exists after a re-read of the system shows the change. Missing evidence stops the reply. |
| A catch-all rule may **ask**, never **assert** | `kb.py` refuses to load a rule that states a diagnosis without being pinned to a specific state. |
| One choke point for "may the agent write this?" | `safety.should_auto_execute` refuses anything routed to a person, whatever the rule, the model or a CLI flag said. A person's approval can only add a yes on top of the same hard guards. |
| Only allowlisted diagnoses are ever fixed | `FIX_ALLOWLIST` names the one action per diagnosis (`job_stuck` retries the job, `record_never_ended` closes the session). Editing the YAML can not make another fix automatic. |
| Auto-fix only with a track record | The rule opts in, confidence is HIGH, and the rule has been confirmed solved at least 5 times. Until then a person approves. A rule approved from the proposals queue starts at zero. |
| A record is fixed at most once | The `fix_ledger` table has the record as its primary key and refuses deletes and rewrites, so a second ticket about the same record can not fix it again. The `attempts` table keeps one try per ticket per record. |
| Refused rows say why | Every record the agent did not touch gets a row in `row_skips` with the reason. |
| Do not guess who someone is | An abbreviated name ("DaWh") or two matching accounts goes to a person. |
| Compare names after cleaning them | Zero-width spaces, byte-order marks, non-breaking spaces and look-alike letters are removed before names are matched. |
| Do not guess at a record with two problems | A record with more than one diagnosis goes to a person; only a record whose *only* problem is the missing end time is ever fixed. |
| A system that did not answer is not a "no" | An unreachable or disagreeing system sends the issue to a person with the reason instead of a guess. |
| Branch on data, not labels | `analyze.py` decides from what the systems show; the model's ticket type only picks the reply. |
| Probing must not break the session | Opening the provisioning portal's or the activity log's login page logs you out (the mocks do this on purpose). Adapters probe `/` and recover once if a session dies. |
| A read right after a write can be stale | After a fix, poll with a deadline; report "not confirmed" rather than guess. |
| A review that did not run is not a pass | Timeouts, refusals and unparseable answers are `ERROR`, kept apart from `FLAG`, and both block the send. An `ERROR` is not revised. |
| Revision is bounded and re-reviewed | At most 2 rounds; every revision goes back through the full reviewer. |
| Reviewer judges words, not recipients | Email and CC fields are stripped before the reviewer sees the facts. |
| House style | Every reply opens "Hi there,", and em dashes and "also" are rejected because they read as machine-written. |
| Nothing is processed twice | A seen-ticket registry skips tickets already handled unless the requester has commented since. |
| Two runs never share a browser | Leases in SQLite: one live run per ticket and per worker, across processes. |

## Install and try it (about 5 minutes, no accounts needed)

You need **Python 3.10 or newer**. Check with `python3 --version`. On Windows, use `python`
instead of `python3` and `.venv\Scripts\activate` instead of `source .venv/bin/activate`.

1. **Get the code**
   ```bash
   git clone https://github.com/JPInert/helpdesk-agent-kit.git
   cd helpdesk-agent-kit
   ```
2. **Make a private Python environment and install the parts**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   python -m playwright install chromium
   ```
   The last line downloads the browser the agent drives for the provisioning portal (about 150 MB).
3. **Run the demo.** It starts the three mock systems, works 19 demo tickets and prints what it did.
   Nothing leaves your machine.
   ```bash
   python -m helpdesk_kit demo --show-replies
   ```
4. **Let it fix things.** Same tickets, but now it is allowed to write to the mock systems:
   ```bash
   python -m helpdesk_kit demo --apply --approve --show-replies
   ```
   `--approve` stands in for a person approving the fix, because a brand-new rule has no track
   record yet. It approves every fix in that run, so use it per ticket (`run --ticket N`) on a
   real queue, not on `queue`. The safer way is the dashboard's per-ticket Approve button.
5. **Look at the systems yourself** (optional): `python -m helpdesk_kit portal`, then open
   <http://127.0.0.1:8750> (provisioning), <http://127.0.0.1:8751> (approvals) or
   <http://127.0.0.1:8752> (activity log) and sign in as `demo` / `demo-password`.
6. **Open the review dashboard** on what the demo just did (optional). Pick a long random
   value for the token (at least 16 characters); the dashboard will not start without one:
   ```bash
   DASHBOARD_TOKEN=<your own random value> python -m helpdesk_kit dashboard --db data/demo.db
   ```
   then open <http://127.0.0.1:8760>, sign in with that value, and open a ticket.
7. **Run the tests**: `python -m pytest`

Other commands: `proposals` (list, `approve N`, `reject N`), `followup`, `stats`, `queue --resume latest`,
`events --ticket N`.

What the demo prints (dry run, from my last run):

```
ticket   type                  state                  rule                          fixed  status                 why
1015     SESSION_REJECTED      outside_approval_dates session-outside-approval      -      REPLY_READY
1003     LICENSE_NOT_ASSIGNED  seat_pool_exhausted    seat-pool-full                -      REPLY_READY
1012.1   SESSION_REJECTED      record_never_ended     session-never-ended           -      AWAITING_APPROVAL
1012.2   SESSION_REJECTED      resource_not_approved  session-resource-not-approved -      HUMAN_REVIEW_REQUIRED  rule session-resource-not-approved always goes to a person
         ticket 1012: HUMAN_REVIEW_REQUIRED (2 units, one merged reply)
1001     LICENSE_NOT_ASSIGNED  job_stuck              license-stuck                 -      AWAITING_APPROVAL
1010     -                     -                      -                             -      DUPLICATE              duplicate of #1001
1002     ACCESS_PENDING        job_stuck              access-stuck-for-someone      -      AWAITING_APPROVAL
1006     ACCESS_PENDING        unresolvable_person    catch-all                     -      HUMAN_REVIEW_REQUIRED  name 'DaWh' is abbreviated; not guessing who it is; rule catc...
1013     SESSION_REJECTED      weekly_cap_exceeded    session-weekly-cap            -      REPLY_READY
1014     SESSION_REJECTED      wrong_week_start       session-wrong-week-start      -      HUMAN_REVIEW_REQUIRED  rule session-wrong-week-start always goes to a person
1016.1   SESSION_REJECTED      overlapping_sessions   catch-all                     -      HUMAN_REVIEW_REQUIRED  rule catch-all always goes to a person
1016.2   SESSION_REJECTED      overlapping_sessions   catch-all                     -      HUMAN_REVIEW_REQUIRED  rule catch-all always goes to a person
         ticket 1016: HUMAN_REVIEW_REQUIRED (2 units, one merged reply)
1017.1   LICENSE_NOT_ASSIGNED  job_stuck              license-stuck                 -      AWAITING_APPROVAL
1017.2   SESSION_REJECTED      record_never_ended     session-never-ended           -      AWAITING_APPROVAL
         ticket 1017: AWAITING_APPROVAL (2 units, no reply)
1018     LICENSE_NOT_ASSIGNED  no_request_found       no-request-on-file            -      REPLY_READY
1019     ACCESS_PENDING        no_request_found       no-request-on-file            -      REPLY_READY
1011     BILLING_QUESTION      cycle_locked           billing-locked                -      HUMAN_REVIEW_REQUIRED  the charge is in an invoiced cycle; billing must approve any ...
1007     POLICY_QUESTION       -                      policy-question               -      HUMAN_REVIEW_REQUIRED  rule policy-question always goes to a person
1009     HARDWARE              -                      hardware-vendor               -      REPLY_READY
1004     ACCOUNT_SUSPENDED     account_suspended      account-suspended             -      REPLY_READY
1005     LICENSE_NOT_ASSIGNED  all_ok                 already-active                -      REPLY_READY
1008     -                     -                      -                             -      SKIPPED                machine_generated
```

Tickets 1012, 1016 and 1017 show the multi-record and multi-issue cases: `1012.1` and `1012.2` are two
rejected records in one ticket, each matched and fixed on its own, and `1017.1` and `1017.2` are two
separate problems in one ticket (a stuck license request and a rejected session). Ticket 1016 has no
rule for its diagnosis, so it goes to a person and files a rule proposal. After the table the demo
prints the field-level findings, the ledger and skipped rows, the proposals queue, a sample of the
reply stage, a follow-up timeline on a simulated clock, the queue after a requester answers, and
the aggregate stats.

The demo runs **offline**: a keyword classifier stands in for the model, each demo screenshot
has a JSON file with what a vision model would extract, the reviewer is a deterministic one, and
the rule-drafting is a template. Add `--llm` (with `ANTHROPIC_API_KEY` set) to use Claude for the
classifier, the reviewer, the reviser and vision.

## Use it on your own Zendesk

1. Copy `.env.example` to `.env` and fill in `ZENDESK_SUBDOMAIN`, `ZENDESK_EMAIL`,
   `ZENDESK_API_TOKEN` (Zendesk Admin Center → Apps and integrations → Zendesk API) and
   `ZENDESK_VIEW_ID` (the number at the end of the view's URL). Add `ANTHROPIC_API_KEY`:
   live mode refuses to run without it, because the offline classifier only knows the demo.
   A free Zendesk trial works.
2. Read one ticket without changing anything: `python -m helpdesk_kit run --live --ticket 123`
3. Work your view: `python -m helpdesk_kit queue --live --post note` triages the view, then puts
   each drafted reply on its ticket as an **internal note** for a person to send. If the run is
   interrupted, `queue --resume latest` picks up where it stopped.

To point it at your own systems, write one adapter per system (login, session handling, a
`collect` that returns facts), register it in `helpdesk_kit/systems/__init__.py`, and edit
`kb/rules.yaml`. The rest of the pipeline does not change. Adding a rule needs no code.
`python -m helpdesk_kit events --ticket 123` shows what every stage did.

## What is proven

Everything below was run against the **mock systems and fixtures in this repo**. None of the three
systems is a real product, so passing shows the code handles the quirks I built into the mocks,
not that any real system behaves that way.

| Checked | Result |
|---|---|
| `python -m pytest` | 261 tests pass, including 34 that drive headless Chromium against the mock portal. The 48 from v1 still pass; one of them changed signature (see below). New tests cover: every diagnosis rule and the priority between them; approval parsing in several formats and Unicode cleaning; field-level findings; the adapters' session traps (expired session renewed once, login-page probe, CSRF, revalidation); facts merged with provenance and conflicts; the choke point and the allowlist; the ledger refusing a second fix, a delete and a rewrite; per-row skips; multi-issue tickets fixed independently and answered in one message; proposals (dedupe, approve, reject, acceptance rate, starting in approve-each mode and graduating at 5 solves); merge, review, bounded revise, send (sign-off kept with the name after a list, links, sanity words); follow-up timing; queue ordering, resume after an interrupted run and the seen registry; the dashboard's token header, cookie and CSRF rules; stats output with no names or ids. |
| `python -m helpdesk_kit demo` | All 19 demo tickets route as shown above, in about 11 seconds on my machine. |
| `python -m helpdesk_kit demo --apply --approve` | The approved fixes are applied and confirmed by re-reading the systems (5 records in the ledger, all confirmed), in about 24 seconds. |
| Review dashboard | Tested with Flask's test client, and started once as a real server and called with `curl` using a token. I did **not** write or run the browser userscript; the two routes it would call are what is tested. |
| Claude calls | Wiring tested with fake clients and the SDK's own schema conversion: the classifier, the reviewer and the reviser. **Not yet run against the live API in this repo.** |
| Live Zendesk | **Not yet run against a real Zendesk instance from this repo.** The client uses only documented public endpoints and is tested on fixtures shaped like their responses. |

The one v1 test I changed is `test_auto_execute_needs_opt_in_high_confidence_and_a_track_record`: the
choke point now also takes the diagnosis state, so its calls pass `state=...`. Its assertions are the same.

## Layout

```
helpdesk_kit/   the agent: one module per stage, plus cli.py, pipeline.py, demo.py
  systems/      one adapter per back-office system, and the registry
mock_portal/    three fake systems (Flask): provisioning, approvals, activity log, traps documented at the top of each
kb/rules.yaml   the knowledge base: 17 rules, edit this to change what it does
fixtures/       19 synthetic tickets shaped like Zendesk's API, and 2 screenshots
tools/          make_fixtures.py regenerates fixtures/; stats.py prints aggregate stats
docs/           ARCHITECTURE.md
tests/          unit tests and browser end-to-end tests
```

## Not included

* Any real portal, credentials or data. Every person, company and address is invented (`.test` domains).
* A browser userscript for canned replies. The dashboard has the two token-protected routes it would call.
* Proof against a real Zendesk or a live model (see above).
* Tickets about more than one person. All issues in one ticket are treated as being about the same person.
* Time zones. Approval dates and session times are UTC calendar dates.
* A model drafting the proposed rules. The drafts are templates filled from the evidence; a person edits and approves them.
* Real deployment hardening for the dashboard: it is one shared token, bound to `127.0.0.1`, meant for one person on one machine.

## Built with Claude Code

Written with Claude Code as a clean-room rewrite: no code or data from the original is in this repo. Everything in "What is proven" was run on the mocks before it was written down.

## License

MIT. See [LICENSE](LICENSE).
