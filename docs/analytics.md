# Doctor analytics

URL: `/doctor/analytics`; JSON: `/api/doctor/analytics`. Both use the existing
`doctor_identity` grant validation. They do not introduce a role or bypass login.
Navigation is next to AI expenses. Owner/doctor PWA IDs, manifests and start URLs
remain separate.

## Storage and rollout

`analytics_events` stores event ID, internal user ID (nullable), random anonymous
session ID, random session ID, name, source, UTC timestamp and allowlisted JSON.
No messages, filenames, contacts, IP addresses, tokens or user agents are copied.
Indexes cover user, event, source, timestamp and common combinations.
`analytics_state.v1` records when observation started.

Startup performs an additive, idempotent migration under the existing PostgreSQL
advisory lock (728190423). It creates two tables and a payment status/paid-at index,
then backfills verifiable facts in batches of 300 using deterministic IDs. No
existing user/payment data is rewritten. Bot and web share the journal. Web workers
run a read-only aggregate smoke check before becoming healthy. Migration logs only
counts, not patient or account data. A code rollback can leave these additive tables
in place; do not delete event history.

Business events are observed at SQLAlchemy `after_flush`, written using a savepoint
in the same transaction, and disappear if the business transaction rolls back.
Analytics write failures are isolated from business writes. Navigation is a small
separate HTTP request; Telegram DB work runs via `asyncio.to_thread`.

## Sources and identity

`telegram`, `web`, `app`, plus explicit `unknown` for unverifiable history.
App means the installed standalone PWA, not an ordinary mobile browser. JS reads
standalone mode, supplies the source on page events, submits and chat requests.
The source says where the action happened, not a marketing acquisition channel.
A Telegram checkout opened on the website belongs to Web/App. Later payment
confirmation inherits that checkout source, not the approving doctor's platform.

Analytics uses a separate signed HttpOnly/Secure/SameSite cookie. It never writes
owner authentication state. The navigation endpoint does not refresh or overwrite
Flask's login cookie, preventing a delayed beacon from restoring a stale login.
Navigation has a signed session-bound CSRF proof, allowlisted names and a bounded
per-worker rate limiter. Clients cannot submit registrations, payments, arbitrary
user IDs, timestamps or metadata. Doctor API responses are private/no-store.

Sessions rotate after 30 minutes idle and when switching accounts. Anonymous events
within the current session are linked on successful login. Shared existing internal
user IDs deduplicate Telegram, Web and App. Separate accounts are not guessed to be
the same person. Anonymous sessions are shown separately from known unique users.
Telegram cannot observe silent chat opens; a handled private interaction begins a
session. Doctor navigation does not produce customer navigation events.

## Events

- `app_open`, `registration`, `login`, `pet_created`
- `assistant_open`, `ai_question`, `analysis_uploaded`
- `consultation_open`, `consultation_created`
- `pricing_open`, `payment_started`, `payment_success`, `payment_failed`
- `pets_open`, `analyses_open`, `prevention_open`

`ai_question` means an accepted unique credit-ledger request, including documents
and provider failures. Idempotent replay does not produce another event. Invalid
inputs/credit denials are not accepted requests. Telegram analysis upload is logged
when an accepted document request is reserved; Web upload when its document is saved.
Payment failure includes rejection/cancellation, not an uncompleted started payment.

## Historical coverage

Registrations, pets, consultation requests, accepted credit-ledger requests, stored
web documents, order creation and dated confirmed payments can be backfilled.
Saved Web chat answers missing an exact ledger reference are recoverable. Legacy
Telegram answers are included only before the first credit-ledger record, because
later answers lack an exact join key and could duplicate ledger requests. Older
failed AI attempts, open pages, sessions, payment rejection times and source
attribution cannot be invented. Unknown source records still count in overall totals.

## Metrics

- Today/7/30 days use calendar days; all time starts at the earliest known fact.
- Default timezone: `Asia/Bangkok` (owner's current timezone), explicitly shown.
  `MYDOCTOR_ANALYTICS_TIMEZONE` can use another IANA timezone. Stored times stay UTC.
- Active users are distinct known internal users with events. Registration counts
  come from the user table; channel registration uses its recorded source.
- Hourly today, daily for 7/30 days, monthly for all time beyond 120 days.
- Strict ordered within-period funnel: each stage belongs to the preceding cohort.
  Navigation-dependent conversion begins only at the observation start date.
- Returning: observed sessions both before and within the selected period.
  Multiple-use: more than one observed session within it. No synthetic historical
  retention; all time has no previous period.
- Payments and revenue come directly from `billing_payment_orders`, only status
  `paid` with `paid_at`, filtered by confirmation time. Different currencies are
  never summed or converted with an invented exchange rate. Mean receipt and plan
  mix use the same confirmed-payment set.
- First payer is based on all dated confirmed-payment history. Free-to-paid is the
  share of active users with no payment before the period who pay for the first time.
- Consultation paid flags contain no exact amount and are not package revenue.

## Verification

The analytics tests use an isolated DB and mocked provider responses, never real
bank transfers. They cover source deduplication, identity linking, ordered funnels,
timezone boundaries, repeat sessions, reliable history, replay, rollback, deliberate
collector failures, source attribution of approval, separate currencies, document
and consultation creation, CSRF, role isolation and auth-cookie race protection.
The existing full project suite and PWA worker tests remain deployment gates.
