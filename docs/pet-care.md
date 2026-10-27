# Pet care

`/care` adds four owner workflows: prescribed medication schedules, recovery
observations linked to an operation, a chronic-condition diary, and family access.
Telegram exposes `/care` and “💊 Уход и дневник”, with a single-use authenticated
web login. The bot also runs the independent reminder delivery loop.

## Medication schedules

Owners enter the exact medicine, dose, instructions, local times and date range
from their veterinarian. Photo/PDF recognition creates a reviewable draft and
saves the original; it never enables a schedule. The normal document billing
reservation/refund applies to recognition. Manual schedules and diaries do not
call the model or spend credits.

Each course has a fixed dose, 1–6 daily clock times, an explicit IANA time zone
and at most 180 days. A taper needs separate non-overlapping courses. Missing
clock times are not inferred from prescription frequency. Ambiguous/nonexistent
DST slots are rejected. Existing past slots are not backfilled when a new course
is entered. Times stay tied to the selected time zone during travel.

Doses have durable UTC timestamps. Given/skipped transitions use an atomic
pending-state update and record who marked the dose and when. Future doses
cannot be marked; stopping a course preserves history and cancels pending doses.
The page refreshes visible shared dose statuses every 15 seconds and on focus.
This reduces stale displays; it cannot guarantee that two people will not
administer medicine simultaneously outside the app. Families must check entries
and coordinate before giving a dose.

Reminders require a per-pet/user preference, selectable during course creation.
They go to that account's Telegram ID and/or currently opted-in Web Push devices.
The worker polls every 30 seconds, leases jobs, retries transient failures, and
rechecks access, preferences, device binding and dose status before delivery.
It expires reminders more than an hour late instead of replaying old doses after
an outage. External delivery may be delayed or duplicated after a network timeout;
the diary is authoritative. Notification text contains no medicine or dose details.

## Observations and sharing

Entries contain owner-reported appetite, activity, pain, wound changes, optional
weight/vomiting/seizure counts, a timestamp, notes and a re-encoded private photo.
Missing measurements stay null. Charts show individual observations on the
selected history page, retain missing-data gaps and do not sum repeated daily
entries. Filters and pagination keep historical entries available.

Structured warning signs produce immediate escalation wording. This is a limited
set of prompts, not diagnosis or continuous clinical monitoring. Postoperative escalation wording is informed by the
[ACVS owner guidance](https://www.acvs.org/small-animal/postoperative-care-after-surgery-what-animal-owners-should-expect/).

Only the pet owner creates/revokes a 24-hour, single-use family invitation.
The stored token is hashed; previews do not consume invitations. Up to five
members can access this pet's care profile, prescriptions, observations and
photos, create courses/observations, mark doses and stop reminders. They cannot
invite others or access the owner's private chats, contact information, billing
or unrelated documents. Revocation is checked on every request and reminder.
Historical authorship is retained after a member leaves.

The owner can separately share care with the existing MyDoctor doctor portal.
Both explicit per-pet consent and a prior consultation request are required.
The doctor view is read-only; saving an observation does not send a clinical
message to the veterinarian or imply that it has been read.

## Deployment and verification

Schema changes add `care_*` tables only. Import the models before the existing
advisory-locked `billing.init_schema()` on web and bot startup. No existing
patient, payment or consultation rows are migrated. Rolling back the application
leaves new care data intact but stops the new reminder worker.

Regression coverage is in `tests/test_pet_care.py` and
`tests/pwa-worker.test.cjs`: access revocation, single-use/expired invitations,
CSRF, double dose submission, schedules/DST, OCR review/refunds, private photos,
doctor consent, notification retries/device rebinding and RU/EN rendering.

## Incision photo review

A postoperative entry with a photo has an explicit review button. It uses the
existing vision model (no fine-tuning and no claim of validated diagnostic
accuracy). The normal document tariff applies, with the actual cost shown before
starting. Saving a diary entry remains free. One result is shared by the family;
reopening it does not call AI or spend credits. Model/network/format failures
refund the attempt. Poor or unrelated images produce an unassessable result.

The model receives the current private image and at most two distinct, dated,
earlier photos belonging to the same pet AND operation. Duplicate images are
excluded. The result separates visible findings, photo limitations, optional
comparison and follow-up questions. There are no drug prescriptions or automatic
changes to treatment. The server validates the JSON, prevents comparison without
usable prior photos and cannot downgrade owner-reported urgency. Concerning
features can raise urgency. The saved result describes that observation only,
not the pet's current condition.

A per-entry lease serializes family submissions. Expired leases can be retried
after ten minutes. Permission is checked before and after the remote call;
revocation cancels saving and refunds the attempt. Results stay in the care tables
and the doctor sees them only with the existing consent/consultation gates.

Telegram photo processing uses the same wound-review rules, including for images
without captions. Its multiple crops are explicitly one observation, and previous
text memories are not represented as visual comparisons. Actual dated comparison
is available in /care. Deterministic zero-credit guidance covers described wound
bleeding and separated edges in addition to the existing emergency signs.

Clinical background: ACVS postoperative owner guidance linked above and
[VCA incision care](https://vcahospitals.com/know-your-pet/care-of-surgical-incisions-in-dogs).
These references inform constraints; they are not a training dataset or evidence
of model sensitivity/specificity. Clinical validation on consented, labelled
veterinary cases remains necessary before making diagnostic performance claims.
