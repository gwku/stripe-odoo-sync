from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path


class SyncError(Exception):
    pass


class Rejected(SyncError):
    """The server explicitly rejected a request without committing it."""


class UnknownOutcome(SyncError):
    pass


def require(condition, code):
    if not condition:
        raise SyncError(code)


def log(event, **fields):
    print(json.dumps({"time": dt.datetime.now(dt.UTC).isoformat(), "event": event, **fields}), flush=True)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def object_id(value):
    return value.get("id") if isinstance(value, dict) else value


def relation_id(value):
    return value[0] if isinstance(value, list) else value


def date_of(timestamp, timezone):
    from zoneinfo import ZoneInfo

    return dt.datetime.fromtimestamp(timestamp, ZoneInfo(timezone)).date().isoformat()


def minor(value, decimals):
    amount = Decimal(str(value)) * (Decimal(10) ** decimals)
    rounded = amount.quantize(Decimal(1), rounding=ROUND_HALF_UP)
    require(abs(amount - rounded) < Decimal("0.001"), "odoo_amount_not_in_currency_units")
    return int(rounded)


@contextlib.contextmanager
def state_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncError("another_run_holds_state_lock") from None
        yield


class Ledger:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS operations (
                reference TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                status TEXT NOT NULL, target_id INTEGER, updated_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY, started_at INTEGER NOT NULL,
                finished_at INTEGER, mode TEXT NOT NULL, status TEXT NOT NULL,
                summary TEXT);
            CREATE TABLE IF NOT EXISTS resolutions (
                id INTEGER PRIMARY KEY, reference TEXT NOT NULL,
                resolved_at INTEGER NOT NULL, reason TEXT NOT NULL);
        """)
        self.db.commit()

    def close(self):
        self.db.close()

    def bind(self, scope):
        encoded = json.dumps(scope, sort_keys=True)
        row = self.db.execute("SELECT value FROM settings WHERE key='scope'").fetchone()
        if row:
            require(row[0] == encoded, "ledger_scope_mismatch")
        else:
            with self.db:
                self.db.execute("INSERT INTO settings VALUES ('scope', ?)", (encoded,))

    def get(self, ref):
        return self.db.execute("SELECT * FROM operations WHERE reference=?", (ref,)).fetchone()

    def put(self, ref, fingerprint, status, target_id=None):
        with self.db:
            self.db.execute(
                """INSERT INTO operations VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(reference) DO UPDATE SET fingerprint=excluded.fingerprint,
                status=excluded.status, target_id=excluded.target_id, updated_at=excluded.updated_at""",
                (ref, fingerprint, status, target_id, int(time.time())),
            )

    def ensure(self, ref, fingerprint, lookup, create, verify):
        operation = self.get(ref)
        if operation and operation["status"] != "rejected":
            require(operation["fingerprint"] == fingerprint, "operation_payload_changed")
        target = lookup()
        if target:
            verify(target)
            if operation and operation["target_id"]:
                require(target == operation["target_id"], "operation_target_changed")
            self.put(ref, fingerprint, "succeeded", target)
            return target
        if operation and operation["status"] != "rejected":
            raise UnknownOutcome("operation_result_missing_review_required")
        self.put(ref, fingerprint, "submitted")
        try:
            target = create()
        except Rejected:
            self.put(ref, fingerprint, "rejected")
            raise
        except Exception:
            self.put(ref, fingerprint, "unknown")
            raise UnknownOutcome("odoo_write_outcome_unknown") from None
        # Persist the ID before verification so an amount mismatch is recoverable without another create.
        self.put(ref, fingerprint, "created", target)
        verify(target)
        self.put(ref, fingerprint, "succeeded", target)
        return target

    def permit_retry(self, ref, reason):
        operation = self.get(ref)
        require(
            operation is not None
            and operation["status"] in ("unknown", "submitted")
            and operation["target_id"] is None,
            "only_unresolved_writes_can_be_reset",
        )
        require(bool(reason.strip()), "resolution_reason_required")
        with self.db:
            self.db.execute(
                "INSERT INTO resolutions (reference, resolved_at, reason) VALUES (?, ?, ?)",
                (ref, int(time.time()), reason),
            )
            self.db.execute(
                "UPDATE operations SET status='rejected', updated_at=? WHERE reference=?",
                (int(time.time()), ref),
            )


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Http:
    def __init__(self, opener=None, sleep=time.sleep):
        self.opener = opener or urllib.request.build_opener(NoRedirect())
        self.sleep = sleep

    def request(self, url, headers, payload=None, safe=False):
        body = None if payload is None else json.dumps(payload, allow_nan=False).encode()
        for attempt in range(3 if safe else 1):
            try:
                request = urllib.request.Request(url, data=body, headers=headers)
                with self.opener.open(request, timeout=45) as response:
                    data = response.read(16 * 1024 * 1024 + 1)
                require(len(data) <= 16 * 1024 * 1024, "response_too_large")
                return json.loads(data)
            except urllib.error.HTTPError as error:
                # Never include a response body: it can contain invoice PII, API keys, or tracebacks.
                if safe and error.code in (429, 500, 502, 503, 504) and attempt < 2:
                    self.sleep(2**attempt)
                    continue
                if 400 <= error.code < 500 and error.code != 408:
                    raise Rejected(f"remote_http_{error.code}") from None
                raise UnknownOutcome(f"remote_http_{error.code}_outcome_unknown") from None
            except (urllib.error.URLError, TimeoutError, OSError):
                if safe and attempt < 2:
                    self.sleep(2**attempt)
                    continue
                raise UnknownOutcome("remote_transport_outcome_unknown") from None
            except (json.JSONDecodeError, ValueError):
                raise UnknownOutcome("remote_response_invalid") from None


class Stripe:
    def __init__(self, key, account_id, live, http):
        self.http, self.account_id, self.live = http, account_id, live
        self.headers = {"Authorization": f"Bearer {key}", "Stripe-Version": "2025-03-31.basil"}

    def get(self, path, params=None):
        require(path.startswith("/v1/") and "?" not in path and ".." not in path, "invalid_stripe_path")
        query = urllib.parse.urlencode(params or {}, doseq=True)
        result = self.http.request(
            "https://api.stripe.com" + path + ("?" + query if query else ""), self.headers, safe=True
        )
        if "livemode" in result:
            require(result["livemode"] == self.live, "stripe_mode_mismatch")
        return result

    def list(self, path, params=None):
        params = {**(params or {}), "limit": 100}
        previous = None
        while True:
            page = self.get(path, params)
            require(isinstance(page.get("data"), list), "invalid_stripe_page")
            for item in page["data"]:
                if "livemode" in item:
                    require(item["livemode"] == self.live, "stripe_mode_mismatch")
                yield item
            if not page.get("has_more"):
                break
            require(bool(page["data"]), "empty_stripe_page_with_more")
            cursor = page["data"][-1]["id"]
            require(cursor != previous, "stripe_pagination_not_advancing")
            params["starting_after"] = cursor
            previous = cursor

    def check(self):
        require(self.get("/v1/account")["id"] == self.account_id, "stripe_account_mismatch")
        # Account objects need not expose livemode. Balance does and verifies even an empty account.
        require(self.get("/v1/balance")["livemode"] == self.live, "stripe_mode_mismatch")


class Odoo:
    def __init__(self, url, database, key, company_id, http):
        parsed = urllib.parse.urlsplit(url)
        require(
            parsed.scheme == "https"
            and parsed.hostname
            and not parsed.username
            and not parsed.query
            and not parsed.fragment
            and parsed.path in ("", "/"),
            "invalid_odoo_url",
        )
        self.url, self.http, self.company_id = url.rstrip("/"), http, company_id
        self.headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "digitura-stripe-odoo-sync/1.0",
        }
        if database:
            self.headers["X-Odoo-Database"] = database

    def call(self, model, method, safe=False, **args):
        context = {
            "allowed_company_ids": [self.company_id],
            "default_company_id": self.company_id,
            "tracking_disable": True,
            "mail_create_nosubscribe": True,
            **args.pop("context", {}),
        }
        return self.http.request(
            f"{self.url}/json/2/{model}/{method}", self.headers, {**args, "context": context}, safe=safe
        )

    def search(self, model, domain, fields, limit=100):
        return self.call(model, "search_read", safe=True, domain=domain, fields=fields, limit=limit)

    def one(self, model, identifier, fields):
        records = self.search(model, [["id", "=", identifier]], fields, limit=2)
        require(len(records) == 1, "odoo_record_not_found")
        return records[0]

    def find(self, model, ref, field="ref"):
        domain = [[field, "=", ref], ["company_id", "=", self.company_id]]
        records = self.search(model, domain, ["id"], limit=2)
        require(len(records) <= 1, "duplicate_odoo_reference")
        return records[0]["id"] if records else None

    def create(self, model, values):
        result = self.call(model, "create", vals_list=[values])
        require(
            isinstance(result, list) and len(result) == 1 and type(result[0]) is int,
            "invalid_odoo_create_result",
        )
        return result[0]


def load_config(path):
    config = json.loads(Path(path).read_text())
    required = (
        "stripe_account_id",
        "livemode",
        "start_date",
        "timezone",
        "odoo_url",
        "company_id",
        "sales_journal_id",
        "income_account_id",
        "currencies",
        "tax_map",
        "no_tax",
    )
    require(all(key in config for key in required), "configuration_missing_fields")
    require(type(config["livemode"]) is bool, "invalid_livemode")
    require(
        config["stripe_account_id"].startswith("acct_") and "REPLACE" not in config["stripe_account_id"],
        "configure_stripe_account_id",
    )
    for key in ("company_id", "sales_journal_id", "income_account_id"):
        require(type(config[key]) is int and config[key] > 0, f"configure_{key}")
    dt.date.fromisoformat(config["start_date"])
    from zoneinfo import ZoneInfo

    ZoneInfo(config["timezone"])
    require(config["currencies"], "configure_currencies")
    for currency in config["currencies"].values():
        require(
            type(currency.get("odoo_id")) is int
            and currency["odoo_id"] > 0
            and type(currency.get("decimals")) is int
            and 0 <= currency["decimals"] <= 3,
            "configure_currency_id_and_decimals",
        )
    require(not config.get("sync_payments") or config.get("post_invoices"), "payments_require_posting")
    for key in ("post_invoices", "sync_payments", "receipt_ownership_confirmed"):
        require(type(config.get(key, False)) is bool, f"invalid_{key}")
    if config.get("sync_payments"):
        require(config.get("receipt_ownership_confirmed") is True, "confirm_receipt_import_ownership")
        for key in ("payment_journal_id", "payment_method_line_id"):
            require(type(config.get(key)) is int and config[key] > 0, f"configure_{key}")
    return config
