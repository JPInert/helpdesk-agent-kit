# helpdesk-agent-kit

An AI agent for an IT help desk queue. It reads Zendesk tickets, including the screenshots
people paste in, works out what the person actually needs, looks them up in a back-office
admin portal that has **no API**, fixes the common cases by driving that portal in a real
browser, and drafts a reply that a second model checks before anything is sent.

It is built to be trusted with real tickets. The model never has the last word: the
decisions that matter are made by deterministic code, every claim in a reply has to be backed
by data the run actually collected, and a person stays in the loop until a fix has a track
record.

> **Status: in testing.** It works end to end against the included mock portal and demo
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
ingest ─▶ vision ─▶ classify ─▶ enrich ─▶ analyze ─▶ match ─▶ apply ─▶ reply ─▶ review ─▶ post
 API       model      model      browser    rules      YAML     browser   template  model     API
```

| Stage | What happens | Uses a model? |
|---|---|---|
| **Ingest** | Pulls the ticket, the requester and the **whole thread** from the Zendesk API. Spots tickets that need the screenshot path, skips monitoring alerts, flags duplicates. | No |
| **Vision** | Turns each screenshot into structured fields (screen, exact error text, username, status). | Yes |
| **Classify** | Ticket type, the product involved, and who actually has the problem (a manager often files for someone else). | Yes |
| **Enrich** | Logs into the portal and reads the person's account, jobs and seat pools by calling the portal's own internal JSON from inside the page. | No |
| **Analyze** | Compares what the ticket says with what the portal shows. Pure, deterministic, unit-tested. | No |
| **Match** | Picks a rule from `kb/rules.yaml`: action, confidence, who to copy in, and the reply. | No |
| **Apply** | Clicks Retry in the portal, then **re-reads until the change actually shows**. | No |
| **Reply** | Fills the rule's reply template from collected evidence only. | No |
| **Review** | A second model reads the reply against the verified facts: PASS, FLAG or ERROR. | Yes |
| **Post** | Internal note for a person to send, or a public reply when everything passed. | No |

### The four pieces

| Piece | Where | Highlights |
|---|---|---|
| Ingestion | `helpdesk_kit/ingest.py`, `zendesk.py` | Public Zendesk API only. Detects the three cases the API alone gets wrong: messaging-widget tickets, image attachments, and screenshots pasted into an email (an `<img>` in the HTML with **no** attachment entry). |
| Image parsing | `helpdesk_kit/vision.py` | Structured output from Claude's vision. Strips the model's "no X was visible" sentences, which keyword checks would otherwise read as "X is there", while keeping quoted on-screen text. |
| LLM routing | `classify.py`, `analyze.py`, `kb.py`, `reply.py` | The model classifies; code decides. Most-specific-rule matching, keyword rules for tickets with no record behind them, an auto-execute gate, and a reply reviewer whose failure blocks sending. |
| Browser automation | `helpdesk_kit/portal.py`, `mock_portal/` | Playwright against a portal with no public API, shipped as a mock so you can run it. |

## Design rules it enforces in code

Each of these is a mistake an automation like this makes in real life, and each has a test.

| Rule | How |
|---|---|
| Never claim a fix the run did not confirm | The "it's fixed" reply needs `${new_status}`, which only exists after the portal re-read shows the job done. Missing evidence stops the reply. |
| A catch-all rule may **ask**, never **assert** | `kb.py` refuses to load a rule that states a diagnosis without being pinned to a specific state. |
| Auto-fix only with a track record | Three conditions: the rule opts in, confidence is HIGH, and the rule has been confirmed solved at least 5 times. Until then a person approves. |
| One attempt per record | A portal write is tried once per ticket per record (`attempts` table). An unconfirmed fix goes to a person instead of looping. |
| Do not guess who someone is | An abbreviated name ("DaWh") or two matching accounts goes to a person. |
| Branch on data, not labels | `analyze.py` decides from what the portal shows; the model's ticket type only picks the reply. |
| Probing must not break the session | Opening the portal's login page logs you out (the mock does this on purpose). The client probes `/` and recovers once if a session dies. |
| A read right after a write can be stale | After Retry, poll with a deadline; report "not confirmed" rather than guess. |
| A review that did not run is not a pass | Timeouts, refusals and unparseable answers are `ERROR`, kept apart from `FLAG`, and both block the send. |
| Reviewer judges words, not recipients | Email and CC fields are stripped before the reviewer sees the facts. |
| House style | Every reply opens "Hi there,", and em dashes and "also" are rejected because they read as machine-written. |
| Two runs never share a browser | Leases in SQLite: one live run per ticket and per worker, across processes. |

## Try it (about 5 minutes, no accounts needed)

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
   The last line downloads the browser the agent drives (about 150 MB).
3. **Run the demo.** It starts the mock portal, works 11 demo tickets and prints what it did.
   Nothing leaves your machine.
   ```bash
   python -m helpdesk_kit demo --show-replies
   ```
4. **Let it fix things.** Same tickets, but now it is allowed to press Retry in the portal:
   ```bash
   python -m helpdesk_kit demo --apply --approve --show-replies
   ```
   `--approve` stands in for a person approving the fix, because a brand-new rule has no track
   record yet. It approves every fix in that run, so use it per ticket (`run --ticket N`) on a
   real queue, not on `queue`.
5. **Look at the portal yourself** (optional): `python -m helpdesk_kit portal`, then open
   <http://127.0.0.1:8750> and sign in as `demo` / `demo-password`.
6. **Run the tests**: `python -m pytest`

What the demo prints (dry run):

```
ticket type                  state                rule                      fixed  status                 why
1001   LICENSE_NOT_ASSIGNED  job_stuck            license-stuck             -      AWAITING_APPROVAL
1010   -                     -                    -                         -      DUPLICATE              duplicate of #1001
1002   ACCESS_PENDING        job_stuck            access-stuck-for-someone  -      AWAITING_APPROVAL
1003   LICENSE_NOT_ASSIGNED  seat_pool_exhausted  seat-pool-full            -      REPLY_READY
1004   ACCOUNT_SUSPENDED     account_suspended    account-suspended         -      REPLY_READY
1005   LICENSE_NOT_ASSIGNED  all_ok               already-active            -      REPLY_READY
1006   ACCESS_PENDING        unresolvable_person  catch-all                 -      HUMAN_REVIEW_REQUIRED  name 'DaWh' is abbreviated; ...
1007   POLICY_QUESTION       -                    policy-question           -      HUMAN_REVIEW_REQUIRED  rule policy-question always goes to a person
1008   -                     -                    -                         -      SKIPPED                machine_generated
1009   HARDWARE              -                    hardware-vendor           -      REPLY_READY
1011   BILLING_QUESTION      cycle_locked         billing-locked            -      HUMAN_REVIEW_REQUIRED  the charge is in an invoiced cycle; ...
```

The demo runs **offline**: a keyword classifier stands in for the model, each demo screenshot
has a JSON file with what a vision model would extract, and the reviewer checks house style
only. Add `--llm` (with `ANTHROPIC_API_KEY` set) to use Claude for all three.

## Use it on your own Zendesk

1. Copy `.env.example` to `.env` and fill in `ZENDESK_SUBDOMAIN`, `ZENDESK_EMAIL`,
   `ZENDESK_API_TOKEN` (Zendesk Admin Center → Apps and integrations → Zendesk API) and
   `ZENDESK_VIEW_ID` (the number at the end of the view's URL). Add `ANTHROPIC_API_KEY`:
   live mode refuses to run without it, because the offline classifier only knows the demo.
   A free Zendesk trial works.
2. Read one ticket without changing anything: `python -m helpdesk_kit run --live --ticket 123`
3. Work your view: `python -m helpdesk_kit queue --live --post note` puts each drafted reply
   on its ticket as an **internal note** for a person to send.

To point it at your own portal, replace `helpdesk_kit/portal.py`'s read and Retry methods with
yours and edit `kb/rules.yaml`; the rest of the pipeline does not change. Adding a rule needs
no code. `python -m helpdesk_kit events --ticket 123` shows what every stage did.

## What is proven

| Checked | Result |
|---|---|
| `python -m pytest` | 48 tests pass, including 8 that drive headless Chromium against the mock portal: dry run leaves the portal untouched; approved fix is applied, confirmed by re-reading the portal, and replied to; a fix that fails or cannot be confirmed goes to a person with the reason and is not retried; auto-execute only after 5 confirmed solves; the login-page trap is survived. |
| `python -m helpdesk_kit demo` | All 11 demo tickets route as shown above, in about 6 seconds. |
| Claude calls | Wiring tested with a fake client and the SDK's own schema conversion. **Not yet run against the live API in this repo.** |
| Live Zendesk | **Not yet run against a real Zendesk instance from this repo.** The client uses only documented public endpoints and is tested on fixtures shaped like their responses. |

## Layout

```
helpdesk_kit/   the agent: one module per stage, plus cli.py and pipeline.py
mock_portal/    the fake provisioning portal (Flask), with its traps documented at the top
kb/rules.yaml   the knowledge base: edit this to change what it does
fixtures/       11 synthetic tickets shaped like Zendesk's API, and 2 screenshots
tools/          make_fixtures.py regenerates fixtures/
tests/          unit tests and browser end-to-end tests
```

## License

MIT. See [LICENSE](LICENSE).
