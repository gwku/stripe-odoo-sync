# Stripe to Odoo sync worker

Dependency-free Python 3.13 worker using Stripe's REST API and Odoo 19 JSON-2.
The Kubernetes deployment runs weekly on Monday at 06:00 Amsterdam time.
This standalone repository owns the worker, tests, and container image. Deployment
configuration and rollout instructions remain in the sibling `digitura-iac`
checkout at `deploy/projects/custom/stripe-odoo-sync/README.md`.

Source: [gwku/stripe-odoo-sync](https://github.com/gwku/stripe-odoo-sync).

## Behavior

Each run lists every invoice created from the approved `start_date` through the
run's fixed discovery boundary. This deliberately revisits older invoices for
later finalization, credits, and settlements. A missed week does not lose changes.
All line, credit-note, and payment collections are paginated. Stripe requests use
the explicit `2025-03-31.basil` API version; schema changes require fixture updates
and sandbox verification before changing that version.

The default `dry-run` mode makes no Odoo writes. It checks connectivity, company,
journal, income-account, currency, and supported tax configuration, then validates
source document totals and produces amount previews. It cannot prove Odoo's tax
computation without creating a draft. `apply` creates customer contacts, invoice
drafts, and linked credit-note drafts, then reads back their identity, lines,
accounts, tax groups, and totals. It does not send emails or collect payments.

`post_invoices: true` enables posting only after draft verification. Optional
`sync_payments: true` records supported successful Stripe PaymentIntent
allocations as manual Odoo receipts through the payment-register wizard. It
requires posting, an inbound manual payment method with an outstanding account,
and `receipt_ownership_confirmed: true`. Configure a Stripe clearing journal;
confirm existing bank feeds do not also create those receipts.

Receipt import handles full and partial payments and verifies the remaining
receivable. It rejects a PaymentIntent shared across invoices, overpayments,
customer balances, unsupported payment types, and unmatched external payments.
Pre-payment credits can reconcile against the original invoice. Post-payment
credits, refunds, disputes, and bad-debt treatment require accountant review.
The worker imports credit documents but never invents refund cash movements.

`reconcile` emits Stripe balance-transaction totals by currency and reporting
category. It is a read-only settlement report, not an automatic Odoo bank
reconciler. Fees, payouts, refund cash, and disputes remain owned by the existing
accounting process. No fallback tax, write-off, or balancing line is invented.

Supported taxes are mapped Odoo sales percentage taxes, exclusive of price,
without compound bases and with invoice-based exigibility. Stripe inclusive
prices are normalized to net values. Shipping, billing credit grants, Connect
issuers/transfers, unsupported rounding/precision, and unknown tax treatments
stop the affected invoice for review. Other invoices continue, but the run exits
unsuccessfully so partial failure remains visible.

## Configuration

Copy `config.example.json` to ignored `config.local.json` for local work. The
example's IDs and date are examples, not Digitura's accounting setup. Fill in:

| Setting | Meaning |
| --- | --- |
| `stripe_account_id`, `livemode` | Expected Stripe account and test/live mode; both are checked even for an empty account. |
| `start_date` | Approved invoice **creation-date** cutoff. Do not move it forward to hide failures. Earlier issued invoices require a separate migration decision. |
| `company_id`, `sales_journal_id`, `income_account_id` | Existing Odoo record IDs in the intended company. |
| `currencies` | Currency code to `{odoo_id, decimals}`. Only explicitly configured currencies are accepted. Use Stripe's currency-specific exponent, checked against Odoo. |
| `tax_map` | Stripe tax-rate ID to existing Odoo tax ID, e.g. `{"txr_example": 42}`. |
| `no_tax` | Explicit treatment to Odoo tax-ID list. `{"none": []}` deliberately accepts ordinary untaxed sales; exempt and reverse-charge treatments require reporting tax IDs. |
| `partner_map` | Optional Stripe customer ID to an existing Odoo partner ID. Use for migration matches and changed historical customer snapshots. |
| `document_map` | Optional Stripe invoice/credit-note ID to an existing Odoo move ID, to resolve ambiguous migration matches. Financial verification still applies. |
| `income_account_map` | Optional Stripe product ID to an Odoo income account ID; otherwise use the configured income account. |
| `post_invoices`, `sync_payments` | Boolean opt-ins, both false initially. |

Contacts first use explicit mappings and existing connector references. Otherwise,
the worker searches the intended company and shared contacts, including archived
records. A unique match requires the same normalized name plus VAT number or
email, without conflicting VAT numbers. Name-only, email-only, conflicting,
ambiguous, and archived matches stop for review; no replacement contact is created.
It creates a new contact only when no possible existing contact is found.

Auto-created contacts use the finalized invoice's name, address, and EU VAT ID.
The first snapshot is frozen. A later differing snapshot requires an explicit
`partner_map`. Existing contacts are reused without overwriting their details.
Original document numbers and Stripe PDF links are stored on the Odoo move;
Odoo keeps its own document sequence. PDF bytes are not archived by this worker.

Existing documents first use saved links or the connector's deterministic
reference. For manually entered invoices, automatic matching requires a unique
match on company, customer, invoice type, currency, issue date, net amount, and
total. The worker then verifies the sales journal, the complete multiset of line
net amounts/accounts/tax IDs, and actual tax amounts per tax ID. It permits
different descriptions, line order, and collapsed quantities in a manual invoice
when those financial details agree. Credit notes also need the correct original
invoice link. Ambiguous or incompatible candidates stop for review instead of
creating another invoice. An explicit `document_map` resolves ambiguity but cannot
bypass financial verification.

Contact and document links are stored in SQLite, including during dry runs.
Existing Odoo references, document numbers, lines, and contact details stay intact.
Each Odoo document can belong to only one Stripe document within this ledger.
Every replay revalidates the linked target; a missing or changed target never
falls back to creating a replacement. Uncertain write intents cannot be overridden
by migration matching. Preserve and back up the ledger for scheduled runs.

Adopted invoices and credits are not automatically posted, cancelled, or settled.
Enabling receipt import for a linked invoice, reconciling a linked credit, or
voiding a linked source stops for review so existing accounting is not duplicated.

Set `STRIPE_API_KEY` and `ODOO_API_KEY` through the environment. Stripe requires
read access to Account, Balance, Invoices/lines, Credit Notes/lines, Invoice
Payments, PaymentIntents when importing receipts, and Balance Transactions when
running settlement reports. Use an Odoo integration user scoped to the company
with contact and accounting permissions, and track its API key expiry. External
API entitlement is required on the Odoo subscription.

## Commands

Run from this directory after supplying credentials and an approved configuration:

```sh
python -m stripe_odoo sync --config config.local.json --state ./state/ledger.sqlite3
python -m stripe_odoo sync --mode apply --config config.local.json --state ./state/ledger.sqlite3
python -m stripe_odoo backfill --from-date 2026-01-01 --config config.local.json --state ./state/ledger.sqlite3
python -m stripe_odoo reconcile --config config.local.json --state ./state/ledger.sqlite3
python -m stripe_odoo status --state ./state/ledger.sqlite3
python -m stripe_odoo backup --state ./state/ledger.sqlite3 --output ./state/snapshot.sqlite3
```

`backfill` uses the same importer. Its optional narrower boundary cannot precede
the configured cutoff. Omit it to scan the full approved period. `status` and
`backup` require no API credentials. A nonblocking file lock protects normal runs,
manual jobs, status, recovery, and backup snapshots from concurrent state access.

## Recovery and state retention

SQLite binds the state volume to Stripe account, test/live mode, Odoo URL/database,
and company. It stores durable write intents before Odoo mutations and records
target IDs before verification. Replays verify existing records instead of
creating duplicates. A lost create response is recovered if the exact reference
is found and its accounting fields agree. The same applies to posting and receipt
actions. If a previous write may still complete and no result is found, the
worker stops that operation for review. A local rollback cannot undo an Odoo write.

Use `status` to inspect incomplete operations. Correct mappings or fix the Odoo
draft causing verification errors before rerunning. Do not delete the ledger,
remove reference fields, or reset operation rows to force a retry.

If Odoo shows the result, rerun normally to recover it. If the operation has no
result, an operator must verify that the original request has finished without
any accounting effect before permitting another attempt:

```sh
python -m stripe_odoo resolve --state ./state/ledger.sqlite3 \
  --reference 'EXACT_REFERENCE_FROM_STATUS' \
  --reason 'Verified the original Odoo request finished without creating a record' \
  --confirm-request-finished-without-effect
```

The confirmation permits retry of one unresolved intent and retains an audit
reason. It does not run the sync. Created or successfully verified records cannot
be reset this way. Target-side reference searches are not a database uniqueness
constraint; preserve the ledger and ensure all invocations share its lock.

Backups use SQLite's online backup API and an integrity check. Restore while all
jobs are suspended and no pod is using the volume. Preserve the damaged state
for investigation. A restored ledger can predate Odoo: the importer probes target
references before creating anything, and uncertain or inconsistent records still
require review. Treat the state and its backups as operational accounting data.

## Verification

From the repository root:

```sh
python3 -m unittest discover -s tests -v
uvx ruff@0.17.1 check .
uvx ruff@0.17.1 format --check .
docker build -t stripe-odoo-sync:test .
docker run --rm --read-only stripe-odoo-sync:test --help
```

These tests cover local accounting normalization, migration matching, contact
reuse, ambiguous/changed/deleted targets, replay and crash recovery, partial
receipts, unsafe wizard rejection, pagination, locks, and backup integrity.
They do not replace accounting verification against an Odoo 19 test database.

## Image releases

The included workflow checks pull requests and pushes to `main`. Publishing is
an explicit manual workflow run with `publish` enabled. It builds amd64 and arm64 images under
`ghcr.io/gwku/stripe-odoo-sync`, tagged with `VERSION` and
`sha-<full Git commit>`. Pushes to `main` only check and build locally in CI.

Keep the repository name `stripe-odoo-sync` and the `gwku` owner for
the image path configured in Digitura's deployment. Pin the production
overlay to a published commit tag or image digest. The version tag is a build
alias; do not rely on it as immutable. Deployment and Prometheus checks run
separately in `digitura-iac`. Local tests never push an image.
