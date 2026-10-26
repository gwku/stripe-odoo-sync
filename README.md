# Stripe to Odoo sync

Stripe issues my invoices and collects payments; Odoo is where I keep the
accounts. This project moves the issued documents into Odoo without having to
enter the same invoice twice. It also recognises invoices and contacts that
were entered manually before the integration existed.

It is a Python 3.13 command-line worker, packaged as a Docker image. A run
checks Stripe for invoices and credit notes, validates their accounting mappings,
reuses or creates the corresponding Odoo records, saves the original PDFs, and
exits. SQLite keeps the links and operation history between runs.

I run it as a **weekly Kubernetes CronJob**, with a persistent volume for SQLite,
secrets supplied by a secret manager, and daily state/configuration backups to
AWS S3. That is one way to operate it: you can run the same worker directly with
Python, Docker, cron, a systemd timer, or another job runner with persistent state.

This repository contains the worker, Dockerfile, tests, image-build workflow,
and a [generic Kubernetes example](examples/kubernetes/README.md). My production
manifests, secret-manager integration, monitoring rules, and AWS configuration
live in a separate infrastructure repository and are not included here.

## Quick start with Python

Requirements: Python 3.13 or newer on Linux/macOS, Stripe API access, and an
Odoo 19 database with external API access and an integration user. The worker
uses Stripe's REST API and Odoo's JSON-2 API. It uses only the Python standard library.

```sh
git clone https://github.com/gwku/stripe-odoo-sync.git
cd stripe-odoo-sync
python3 -m venv .venv
. .venv/bin/activate
cp config.example.json config.local.json
```

Fill in `config.local.json` with your own account, company, journal, revenue,
currency, and tax mappings. Supply `STRIPE_API_KEY` and `ODOO_API_KEY` through
your environment or secret manager. Credentials do not belong in the JSON file.

```sh
python -m stripe_odoo sync --mode dry-run \
  --config config.local.json --state ./state/ledger.sqlite3

# Create real Odoo records after checking your mappings.
python -m stripe_odoo sync --mode apply \
  --config config.local.json --state ./state/ledger.sqlite3
```

`apply` creates drafts unless `post_invoices` is enabled. Payment registration is
a separate option. A dry run makes no Odoo writes, but can persist verified links
in the local ledger. Use the **same ledger** for subsequent runs and back it up.

## Run with Docker

The image is published as `ghcr.io/gwku/stripe-odoo-sync` for amd64 and arm64.
Registry access is required if the package is private. You can also build it:

```sh
docker build -t stripe-odoo-sync:local .
mkdir -p state
docker run --rm --read-only \
  --user "$(id -u):$(id -g)" \
  -e STRIPE_API_KEY -e ODOO_API_KEY \
  --mount "type=bind,src=$PWD/config.local.json,dst=/config/config.json,readonly" \
  --mount "type=bind,src=$PWD/state,dst=/state" \
  stripe-odoo-sync:local sync --mode apply
```

The default image user is UID/GID `10001`. The local example above uses your host
UID/GID to write the mounted state directory. In a scheduler, give the container
write access to `/state` and mount `/config/config.json` read-only. Keep API keys
injected at runtime. Pin a published commit tag or image digest for deployments.

## Run in Kubernetes

The [example setup](examples/kubernetes/README.md) supplies a namespace, PVC,
ConfigMap generation, and CronJob. It explains how to supply credentials and
activate a schedule using your own configuration. It is deliberately generic:
it does not contain my company's accounting mappings, private hosts, secrets,
or cloud storage settings.

In my deployment, the sync uses `concurrencyPolicy: Forbid`, automatic write
retries are disabled, and scheduled/manual jobs share the same state volume and
file lock. A separate daily job runs the backup command, uploads the consistent
snapshot and configuration to S3, then downloads both files and verifies their
checksums. Monitoring checks failed and overdue jobs.

You do not need that infrastructure to run the worker. The operational contract
is the same everywhere: retain the ledger, allow one writer at a time, preserve
the configuration, and keep a restorable backup outside the worker's machine.

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
example's IDs and date are placeholders, not an accounting configuration. Fill in:

| Setting | Meaning |
| --- | --- |
| `stripe_account_id`, `livemode` | Expected Stripe account and test/live mode; both are checked even for an empty account. |
| `start_date` | Approved invoice **creation-date** cutoff. Do not move it forward to hide failures. Earlier issued invoices require a separate migration decision. |
| `company_id`, `sales_journal_id`, `income_account_id` | Existing Odoo record IDs in the intended company. |
| `currencies` | Currency code to `{odoo_id, decimals}`. Only explicitly configured currencies are accepted. Use Stripe's currency-specific exponent, checked against Odoo. |
| `tax_map` | Stripe tax-rate ID to existing Odoo tax ID, e.g. `{"txr_example": 42}`. |
| `tax_rate_map` | Optional `"<country>:<percentage>"` to Odoo tax ID, e.g. `{"NL:21": 42}`, for Stripe Tax, which creates a new tax-rate ID per invoice. Used only for tax-rate IDs missing from `tax_map`; an unmapped rate fails the invoice. |
| `period_separator` | Optional word between the dates in a subscription line's period, default `to` (e.g. `tot` for Dutch). |
| `no_tax` | Explicit treatment to Odoo tax-ID list. `{"none": []}` deliberately accepts ordinary untaxed sales; exempt and reverse-charge treatments require reporting tax IDs. |
| `partner_map` | Optional Stripe customer ID to an existing Odoo partner ID. Use for migration matches and changed historical customer snapshots. |
| `document_map` | Optional Stripe invoice/credit-note ID to an existing Odoo move ID, to resolve ambiguous migration matches. Financial verification still applies. |
| `income_account_map` | Optional Stripe product ID to an Odoo income account ID; otherwise use the configured income account. |
| `post_invoices`, `sync_payments` | Boolean opt-ins, both false initially. |
| `sync_pdfs` | Preserve original invoice and credit-note PDFs as Odoo attachments; true by default. |

Fully discounted invoices (total 0) with no tax lines need no `no_tax` treatment.

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
Odoo keeps its own document sequence. Original PDF bytes are saved as private
attachments on the corresponding Odoo move, including matched existing invoices.
A saved attachment reference prevents repeat uploads. Existing invoice attachments
named `Invoice-<Stripe invoice number>.pdf` are also reused on the matched Odoo
invoice. Downloads are limited to 8 MiB and checked for a PDF header before
creating accounting records. The ledger tracks attachment writes and recovers
responses lost after upload. Missing or altered saved attachments stop for review.

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
Archiving a missing original PDF is allowed independently of those accounting actions.

Set `STRIPE_API_KEY` and `ODOO_API_KEY` through the environment. Stripe requires
read access to Account, Balance, Invoices/lines, Credit Notes/lines, Invoice
Payments, PaymentIntents when importing receipts, and Balance Transactions when
running settlement reports. Use an Odoo integration user scoped to the company
with contact, accounting, and attachment permissions, and track its API key expiry. External
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
your deployment's image path. Pin the production
overlay to a published commit tag or image digest. The version tag is a build
alias; do not rely on it as immutable. Deployment and Prometheus checks run
in the separate infrastructure repository. Local tests never push an image.
