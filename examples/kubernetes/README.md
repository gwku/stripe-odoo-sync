# Kubernetes example

This is a portable starting point for your own cluster. Production deployment,
cloud resources, secret-manager configuration, and monitoring are managed outside
this repository. The example uses your cluster's default StorageClass and starts
with a suspended schedule and dry-run mode.

## Configure and install

Run from the repository root:

```sh
cp config.example.json examples/kubernetes/config.json
```

Replace the example values with your own Stripe account, Odoo connection, and
accounting mappings. `config.json` is ignored by Git; keep API keys out of it.
Adjust the schedule/time zone in `cronjob.yaml`, choose a StorageClass in
`pvc.yaml` if needed, and pin the image to a published commit tag or digest.
If the GHCR image is private, create a registry pull Secret and add it under
`imagePullSecrets`, or build and publish the image in your own registry.

Create `examples/kubernetes/.env` locally with these two entries and your real
values. The file is ignored by Git:

```dotenv
STRIPE_API_KEY=REPLACE_WITH_RESTRICTED_STRIPE_KEY
ODOO_API_KEY=REPLACE_WITH_ODOO_INTEGRATION_KEY
```

Create the namespace and credentials, then apply the example:

```sh
kubectl apply -f examples/kubernetes/namespace.yaml
kubectl -n stripe-odoo-sync create secret generic stripe-odoo-credentials \
  --from-env-file=examples/kubernetes/.env
kubectl apply -k examples/kubernetes
```

You can provision the Secret with your own secret manager instead. Do not commit
credential files. The ConfigMap name is hashed by Kustomize and the CronJob's
volume reference is rewritten automatically.

## Run once, then enable the schedule

The initial template is dry-run. Create a manual Job to validate access and mappings:

```sh
kubectl -n stripe-odoo-sync create job --from=cronjob/stripe-odoo-sync sync-check
kubectl -n stripe-odoo-sync logs -f job/sync-check
```

For real imports, change `SYNC_MODE` to `apply` in `cronjob.yaml` and reapply the
Kustomization. `post_invoices` in `config.json` controls posting; without it the
worker creates drafts. `sync_payments` is a separate opt-in requiring its own
journal and ownership settings. Create a new manual Job for the updated template.

When the results are accepted, change `suspend` to `false` and reapply. The example
schedule runs weekly at 06:00 UTC. There is no web service, ingress, or webhook.
Logs are written to stdout; a failed invoice makes the Job exit unsuccessfully.

## State, backups, and restores

Every invocation must use the same PVC at `/state`, including manual Jobs. SQLite
stores customer/invoice links, attachment records, and uncertain-write intents.
Do not delete it to retry a failed operation. `concurrencyPolicy: Forbid` prevents
overlapping scheduled Jobs; the worker's file lock also protects manual runs.

The PVC provides persistence, not an off-machine backup. Schedule a separate Job
using the worker's `backup` command:

```sh
python -m stripe_odoo backup --state /state/ledger.sqlite3 --output /backup/ledger.sqlite3
```

Mount the live state PVC and a separate writable snapshot volume for that Job.
The command takes the shared lock and uses SQLite's online backup API, then checks
integrity. Upload the snapshot and the matching configuration to your chosen
backup store. Verify a download and checksum before treating the backup as
successful. API keys should remain in your secret manager.

For restore, suspend schedules, ensure no worker is using the PVC, and preserve
the current state before replacing it with the verified snapshot/configuration.
Use `status` to inspect recovered links and operations. Configure monitoring for
failed jobs, missed sync runs, and stale backups. Size the PVC, memory limits,
and runtime deadline for your invoice volume and PDF sizes.
