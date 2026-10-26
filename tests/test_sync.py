import base64
import copy
import hashlib
import io
import re
import sqlite3
import tempfile
import unittest
import urllib.error
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from stripe_odoo.__main__ import main
from stripe_odoo.core import (
    Http,
    Ledger,
    Odoo,
    Rejected,
    Stripe,
    SyncError,
    UnknownOutcome,
    minor,
    state_lock,
)
from stripe_odoo.planning import document_plan
from stripe_odoo.worker import Worker


def config():
    return {
        "stripe_account_id": "acct_test",
        "livemode": False,
        "start_date": "2026-01-01",
        "timezone": "Europe/Amsterdam",
        "company_id": 1,
        "sales_journal_id": 2,
        "income_account_id": 3,
        "currencies": {"EUR": {"odoo_id": 4, "decimals": 2}},
        "tax_map": {"txr_vat": 5},
        "no_tax": {"none": []},
        "partner_map": {"cus_test": 6},
        "sync_pdfs": False,
    }


def invoice():
    return {
        "id": "in_test",
        "object": "invoice",
        "customer": "cus_test",
        "customer_name": "Example",
        "currency": "eur",
        "status": "paid",
        "number": "EX-1",
        "created": 1767222000,
        "status_transitions": {"finalized_at": 1767222000},
        "customer_tax_exempt": "none",
        "total": 12100,
        "total_excluding_tax": 10000,
        "amount_remaining": 0,
        "amount_paid": 12100,
        "amount_shipping": 0,
        "starting_balance": 0,
    }


def line():
    return {
        "id": "il_test",
        "currency": "eur",
        "description": "Service",
        "amount": 10000,
        "quantity": 1,
        "discount_amounts": [],
        "taxes": [
            {
                "amount": 2100,
                "tax_behavior": "exclusive",
                "type": "tax_rate_details",
                "tax_rate_details": {"tax_rate": "txr_vat"},
            }
        ],
    }


class FakeStripe:
    def __init__(self):
        self.invoices = [invoice()]
        self.lines = [line()]
        self.credits = []
        self.allocations = []
        self.params = []

    def check(self):
        pass

    def get(self, path, params=None):
        if path.startswith("/v1/tax_rates/"):
            return {"id": path.rsplit("/", 1)[1], "country": "NL", "percentage": 21.0}
        return copy.deepcopy(next(item for item in self.invoices if path.endswith("/" + item["id"])))

    def list(self, path, params=None):
        self.params.append((path, params))
        if path == "/v1/invoices":
            return iter(copy.deepcopy(self.invoices))
        if path.endswith("/lines"):
            return iter(copy.deepcopy(self.lines))
        if path == "/v1/credit_notes":
            return iter(copy.deepcopy(self.credits))
        if path == "/v1/invoice_payments":
            return iter(copy.deepcopy(self.allocations))
        return iter([])


class FakeOdoo:
    def __init__(self):
        self.records = {}
        self.creates = 0
        self.actions = []
        self.lose_response = False
        self.amount_delta = 0

    def find(self, model, ref, field="ref"):
        return next(
            (
                identifier
                for (kind, identifier), record in self.records.items()
                if kind == model and record.get(field) == ref
            ),
            None,
        )

    def create(self, model, values):
        self.creates += 1
        identifier = self.creates + 10
        record = copy.deepcopy(values)
        record["id"] = identifier
        if model == "ir.attachment":
            # Mirror Odoo 19: "datas" is silently dropped on create, only "raw" stores content.
            record.pop("datas", None)
            data = base64.b64decode(record.pop("raw", ""))
            record.update(
                raw=base64.b64encode(data).decode(),
                file_size=len(data),
                checksum=hashlib.sha1(data).hexdigest(),
            )
        if model == "account.move":
            net = sum(
                Decimal(str(item[2]["price_unit"])) * Decimal(str(item[2]["quantity"]))
                for item in record["invoice_line_ids"]
            )
            tax = sum(
                Decimal(str(item[2]["price_unit"])) * Decimal(str(item[2]["quantity"])) * Decimal(".21")
                for item in record["invoice_line_ids"]
                if 5 in item[2]["tax_ids"][0][2]
            )
            record.update(
                amount_total=float(net + tax) + self.amount_delta,
                amount_untaxed=float(net),
                state="draft",
                line_ids=[],
                amount_residual=float(net + tax),
            )
            record["_tax"] = float(tax)
            line_ids = []
            for index, command in enumerate(record["invoice_line_ids"]):
                line_id = identifier * 100 + index
                item = copy.deepcopy(command[2])
                item["tax_ids"] = item["tax_ids"][0][2]
                self.records["account.move.line", line_id] = item
                line_ids.append(line_id)
            record["invoice_line_ids"] = line_ids
        self.records[model, identifier] = record
        if self.lose_response:
            self.lose_response = False
            raise TimeoutError("committed but response lost")
        return identifier

    def one(self, model, identifier, fields):
        if (model, identifier) in self.records:
            return self.records[model, identifier]
        fixtures = {
            "res.company": {"id": 1},
            "account.journal": {"company_id": [1, "Company"], "type": "sale"},
            "account.account": {"company_ids": [1], "account_type": "income", "active": True},
            "account.tax": {
                "company_id": [1, "Company"],
                "type_tax_use": "sale",
                "active": True,
                "price_include": False,
                "amount_type": "percent",
                "amount": 21,
                "include_base_amount": False,
                "tax_exigibility": "on_invoice",
            },
            "res.currency": {"name": "EUR", "decimal_places": 2, "active": True},
            "res.partner": {"company_id": False},
        }
        return fixtures[model]

    def search(self, model, domain, fields, limit=100, context=None):
        if model == "account.move.line":
            record = self.records["account.move", domain[0][2]]
            if domain[1][2] == "product":
                return [
                    {
                        **self.records["account.move.line", identifier],
                        "price_subtotal": float(
                            Decimal(str(self.records["account.move.line", identifier]["quantity"]))
                            * Decimal(str(self.records["account.move.line", identifier]["price_unit"]))
                        ),
                    }
                    for identifier in record["invoice_line_ids"]
                ]
            return (
                [
                    {
                        "tax_line_id": [5, "VAT"],
                        "amount_currency": record["_tax"]
                        * (1 if record["move_type"] == "out_refund" else -1),
                    }
                ]
                if record["_tax"]
                else []
            )

        def matches(record):
            tokens = iter(domain)

            def evaluate():
                token = next(tokens)
                if token in ("|", "&"):
                    left, right = evaluate(), evaluate()
                    return left or right if token == "|" else left and right
                field, operator, value = token
                actual = record.get(field, False)
                if operator == "=ilike":
                    return str(actual or "").casefold() == str(value).casefold()
                if operator == "ilike":
                    pattern = ".*".join(re.escape(part) for part in str(value).split("%"))
                    return bool(re.search(pattern, str(actual or ""), re.IGNORECASE))
                return actual == value

            results = []
            while True:
                try:
                    results.append(evaluate())
                except StopIteration:
                    break
            return all(results)

        return [record for (kind, _), record in self.records.items() if kind == model and matches(record)][
            :limit
        ]

    def call(self, model, method, **args):
        self.actions.append((model, method, args))
        if method == "action_post":
            for identifier in args["ids"]:
                self.records[model, identifier]["state"] = "posted"
        if method == "button_cancel":
            for identifier in args["ids"]:
                self.records[model, identifier]["state"] = "cancel"


class ReceiptOdoo(FakeOdoo):
    def __init__(self):
        super().__init__()
        self.wizard = None
        self.lose_receipt_response = False
        self.editable = True

    def one(self, model, identifier, fields):
        if model == "account.payment.register":
            return {**self.wizard, "can_edit_wizard": self.editable}
        return super().one(model, identifier, fields)

    def call(self, model, method, **args):
        if model == "account.payment.register" and method == "create":
            self.wizard = args["vals_list"][0]
            self.wizard["invoice_id"] = args["context"]["active_ids"][0]
            return [90]
        if model == "account.payment.register" and method == "action_create_payments":
            move_id = self.wizard["invoice_id"]
            self.records["account.payment", 91] = {
                "memo": self.wizard["communication"],
                "amount": self.wizard["amount"],
                "currency_id": self.wizard["currency_id"],
                "journal_id": self.wizard["journal_id"],
                "state": "in_process",
                "reconciled_invoice_ids": [move_id],
            }
            self.records["account.move", move_id]["amount_residual"] -= self.wizard["amount"]
            if self.lose_receipt_response:
                self.lose_receipt_response = False
                raise TimeoutError()
            return {"res_id": 91}
        return super().call(model, method, **args)


class PlanningTests(unittest.TestCase):
    def plan(self, source=None, lines=None, settings=None, original=None):
        return document_plan(
            settings or config(), source or invoice(), invoice(), lines or [line()], 6, original
        )

    def test_tax_and_quantity_and_minor_currency(self):
        values, expected = self.plan()
        self.assertEqual(values["invoice_line_ids"][0][2]["price_unit"], 100)
        self.assertEqual(expected, {"net": 10000, "total": 12100, "taxes": {5: 2100}, "decimals": 2})
        self.assertEqual(minor(123, 0), 123)

    def test_fractional_quantity_is_preserved(self):
        item = line()
        item["quantity_decimal"] = "2.5"
        values, _ = self.plan(lines=[item])
        self.assertEqual(values["invoice_line_ids"][0][2]["quantity"], 2.5)
        self.assertEqual(values["invoice_line_ids"][0][2]["price_unit"], 40)

    def test_inclusive_tax_is_normalized_to_net(self):
        item = line()
        item["amount"] = 12100
        item["taxes"][0]["tax_behavior"] = "inclusive"
        self.assertEqual(self.plan(lines=[item])[1]["net"], 10000)

    def test_discount_not_applied_twice(self):
        item, source = line(), invoice()
        item["discount_amounts"] = [{"amount": 1000}]
        item["taxes"][0]["amount"] = 1890
        source.update(total=10890, total_excluding_tax=9000)
        self.assertEqual(self.plan(source, [item])[1]["net"], 9000)

    def test_unknown_tax_blocks_before_create(self):
        settings = config()
        settings["tax_map"] = {}
        with self.assertRaisesRegex(SyncError, "tax_mapping_required"):
            self.plan(settings=settings)

    def test_source_total_mismatch_blocks(self):
        source = invoice()
        source["total"] += 1
        with self.assertRaisesRegex(SyncError, "gross_total_mismatch"):
            self.plan(source)

    def test_credit_retains_original_relation(self):
        source = invoice()
        source.update(id="cn_test", object="credit_note", status="issued")
        values, _ = self.plan(source, original=22)
        self.assertEqual(values["move_type"], "out_refund")
        self.assertEqual(values["reversed_entry_id"], 22)

    def test_exemption_needs_explicit_reporting_mapping(self):
        source = invoice()
        source.update(customer_tax_exempt="reverse", total=10000)
        item = line()
        item["taxes"] = []
        with self.assertRaisesRegex(SyncError, "no_tax_treatment_mapping_required"):
            document_plan(config(), source, source, [item], 6)

    def test_zero_total_does_not_imply_payment(self):
        source, item = invoice(), line()
        source.update(total=0, total_excluding_tax=0)
        item.update(amount=0, taxes=[])
        self.assertEqual(self.plan(source, [item])[1]["total"], 0)

    def test_shipping_and_credit_grants_require_review(self):
        source = invoice()
        source["amount_shipping"] = 20
        with self.assertRaisesRegex(SyncError, "shipping"):
            self.plan(source)
        item = line()
        item["pretax_credit_amounts"] = [{"type": "credit_balance_transaction", "amount": 20}]
        with self.assertRaisesRegex(SyncError, "credit_grant"):
            self.plan(lines=[item])


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.ledger = Ledger(self.path)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.ledger.close)

    def test_committed_write_with_lost_response_recovers_without_duplicate(self):
        records, calls = {}, []

        def create():
            calls.append(1)
            records["id"] = 23
            raise TimeoutError()

        with self.assertRaises(UnknownOutcome):
            self.ledger.ensure("ref", "hash", lambda: records.get("id"), create, lambda _: None)
        target = self.ledger.ensure("ref", "hash", lambda: records.get("id"), create, lambda _: None)
        self.assertEqual(target, 23)
        self.assertEqual(len(calls), 1)

    def test_missing_unknown_write_never_retries(self):
        self.ledger.put("ref", "hash", "submitted")
        with self.assertRaises(UnknownOutcome):
            self.ledger.ensure("ref", "hash", lambda: None, lambda: self.fail("unsafe retry"), lambda _: None)

    def test_definitive_rejection_can_retry(self):
        with self.assertRaises(Rejected):
            self.ledger.ensure(
                "ref",
                "hash",
                lambda: None,
                lambda: (_ for _ in ()).throw(Rejected("invalid")),
                lambda _: None,
            )
        self.assertEqual(self.ledger.ensure("ref", "hash", lambda: None, lambda: 9, lambda _: None), 9)

    def test_corrected_payload_can_retry_after_confirmed_rejection(self):
        self.ledger.put("ref", "invalid_payload", "rejected")
        self.assertEqual(
            self.ledger.ensure("ref", "corrected_payload", lambda: None, lambda: 9, lambda _: None), 9
        )

    def test_manual_retry_requires_confirmation_and_retains_audit_reason(self):
        self.ledger.put("ref", "hash", "unknown")
        with patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(
                main(
                    ["resolve", "--state", str(self.path), "--reference", "ref", "--reason", "Checked Odoo"]
                ),
                1,
            )
            self.assertEqual(
                main(
                    [
                        "resolve",
                        "--state",
                        str(self.path),
                        "--reference",
                        "ref",
                        "--reason",
                        "Checked Odoo",
                        "--confirm-request-finished-without-effect",
                    ]
                ),
                0,
            )
        self.assertEqual(self.ledger.get("ref")["status"], "rejected")
        self.assertEqual(
            self.ledger.db.execute("SELECT reason FROM resolutions").fetchone()[0], "Checked Odoo"
        )

    def test_changed_payload_and_different_scope_block(self):
        self.ledger.put("ref", "old", "succeeded", 4)
        with self.assertRaisesRegex(SyncError, "payload_changed"):
            self.ledger.ensure("ref", "new", lambda: 4, lambda: 5, lambda _: None)
        self.ledger.bind({"company": 1})
        with self.assertRaisesRegex(SyncError, "scope_mismatch"):
            self.ledger.bind({"company": 2})

    def test_process_lock_blocks_manual_overlap(self):
        with state_lock(self.path), self.assertRaisesRegex(SyncError, "holds_state_lock"):
            with state_lock(self.path):
                self.fail("second lock acquired")

    def test_sqlite_backup_and_status_without_credentials(self):
        self.ledger.put("ref", "hash", "unknown")
        backup = Path(self.temp.name) / "backup.sqlite3"
        with patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(main(["backup", "--state", str(self.path), "--output", str(backup)]), 0)
            self.assertEqual(main(["status", "--state", str(self.path)]), 0)
        with closing(sqlite3.connect(backup)) as db:
            self.assertEqual(db.execute("SELECT status FROM operations").fetchone()[0], "unknown")


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("sys.stdout", new=io.StringIO()))
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.temp.name) / "state.sqlite3")
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.ledger.close)
        self.settings, self.stripe, self.odoo = config(), FakeStripe(), FakeOdoo()

    def worker(self, apply=True):
        return Worker(self.settings, self.stripe, self.odoo, self.ledger, apply)

    def test_replay_and_later_payment_do_not_duplicate_invoice(self):
        self.worker().run()
        self.stripe.invoices[0]["amount_paid"] = 0
        self.worker().run()
        self.assertEqual(self.odoo.creates, 1)
        self.assertEqual(self.stripe.params[0][1]["created[gte]"], 1767222000)

    def test_customer_creation_is_stable_and_snapshot_changes_are_blocked(self):
        self.settings["partner_map"] = {}
        self.worker().run()
        self.worker().run()
        self.assertEqual(self.odoo.creates, 2)
        self.stripe.invoices[0]["customer_name"] = "Changed customer"
        self.assertEqual(self.worker().run()["failed"], 1)
        self.assertEqual(self.odoo.creates, 2)

    def test_dry_run_has_no_remote_writes_or_operation_intents(self):
        self.worker(False).run()
        self.assertEqual(self.odoo.creates, 0)
        self.assertEqual(self.odoo.actions, [])
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM operations").fetchone()[0], 0)

    def test_loss_after_commit_is_recovered_on_next_run(self):
        self.odoo.lose_response = True
        self.assertEqual(self.worker().run()["failed"], 1)
        self.assertEqual(self.worker().run()["verified"], 1)
        self.assertEqual(self.odoo.creates, 1)

    def test_amount_mismatch_never_posts_or_recreates(self):
        self.settings["post_invoices"] = True
        self.odoo.amount_delta = 0.01
        self.assertEqual(self.worker().run()["failed"], 1)
        self.worker().run()
        self.assertEqual(self.odoo.creates, 1)
        self.assertEqual(self.odoo.actions, [])

    def test_post_is_idempotent_and_paid_state_never_creates_receipt_by_default(self):
        self.settings["post_invoices"] = True
        self.worker().run()
        self.worker().run()
        self.assertEqual([action[1] for action in self.odoo.actions], ["action_post"])

    def test_void_cancels_draft_but_never_posted_record(self):
        self.worker().run()
        self.stripe.invoices[0]["status"] = "void"
        self.worker().run()
        self.assertEqual(self.odoo.records["account.move", 11]["state"], "cancel")
        self.odoo.records["account.move", 11]["state"] = "posted"
        self.assertEqual(self.worker().run()["failed"], 1)

    def test_paid_credit_only_does_not_generate_cash(self):
        source = invoice()
        source.update(id="cn_test", object="credit_note", status="issued", pre_payment_amount=12100)
        self.stripe.credits = [source]
        self.worker().run()
        self.assertEqual(self.odoo.creates, 2)
        self.assertFalse(any(key[0] == "account.payment" for key in self.odoo.records))

    def test_existing_line_tampering_is_detected_even_when_totals_agree(self):
        self.worker().run()
        self.odoo.records["account.move.line", 1100]["account_id"] = 99
        self.assertEqual(self.worker().run()["failed"], 1)

    def test_payment_credit_and_external_settlement_guards(self):
        source = invoice()
        source["starting_balance"] = -12100
        with self.assertRaisesRegex(SyncError, "external_payment_requires_review"):
            self.worker().payments(source, 11)
        with self.assertRaisesRegex(SyncError, "post_payment_credit"):
            self.worker().reconcile_credit({"total": 12100, "pre_payment_amount": 0}, 12, 11)

    def test_partial_failure_still_processes_other_invoices(self):
        bad = invoice()
        bad["id"], bad["total"] = "in_bad", 999
        self.stripe.invoices.append(bad)
        result = self.worker().run()
        self.assertEqual(result["verified"], 1)
        self.assertEqual(result["failed"], 1)


class DescriptionTests(unittest.TestCase):
    def names(self, **changes):
        item = {**line(), "period": {"start": 1767222000, "end": 1769900400}, **changes}
        values, _ = document_plan(config(), invoice(), invoice(), [item], 6)
        return [command[2]["name"] for command in values["invoice_line_ids"]]

    def test_one_off_line_keeps_stripe_description_without_period(self):
        self.assertEqual(
            self.names(description="Hours (uren, 24 oktober 2026)"), ["Hours (uren, 24 oktober 2026)"]
        )

    def test_subscription_line_uses_pdf_name_and_adds_period(self):
        parent = {"parent": {"type": "subscription_item_details"}}
        for description in ("1 × Zorgeloos (voor €139.00 / maand)", "1 × Zorgeloos (at €139.00 / month)"):
            self.assertEqual(
                self.names(description=description, **parent),
                ["Zorgeloos [2026-01-01 to 2026-02-01]"],
            )
        self.assertEqual(
            self.names(description="Custom subscription text", **parent),
            ["Custom subscription text [2026-01-01 to 2026-02-01]"],
        )


class DiscountTests(unittest.TestCase):
    def plan(self, labels=None, **changes):
        source = {**invoice(), "total_excluding_tax": 7500, "total": 9075}
        item = {**line(), "discount_amounts": [{"amount": 2500, "discount": "di_1"}], **changes}
        item["taxes"] = [{**item["taxes"][0], "amount": 1575}]
        values, expected = document_plan(config(), source, source, [item], 6, discount_labels=labels)
        return values["invoice_line_ids"], expected

    def test_discount_becomes_a_negative_line_with_the_same_tax(self):
        commands, expected = self.plan({"di_1": "WELCOME (25% off)"})
        self.assertEqual([c[2]["name"] for c in commands], ["Service", "WELCOME (25% off)"])
        self.assertEqual([c[2]["price_unit"] for c in commands], [100.0, -25.0])
        self.assertEqual(commands[0][2]["tax_ids"], commands[1][2]["tax_ids"])
        self.assertEqual(expected["net"], 7500)

    def test_unlabelled_discount_falls_back_to_a_generic_name(self):
        commands, _ = self.plan()
        self.assertEqual(commands[1][2]["name"], "Discount")

    def test_worker_labels_discounts_from_the_coupon_and_tolerates_unreadable_coupons(self):
        class Stripe:
            def get(self, path):
                if path == "/v1/coupons/ok":
                    return {"name": "FREE", "percent_off": 100.0}
                raise Rejected("remote_http_403")

        worker = Worker.__new__(Worker)
        worker.stripe = Stripe()
        lines = [
            {
                "discounts": [
                    {"id": "di_ok", "source": {"coupon": "ok"}},
                    {"id": "di_hidden", "source": {"coupon": "hidden"}},
                    "di_not_expanded",
                ]
            }
        ]
        self.assertEqual(worker.discount_labels(lines), {"di_ok": "FREE (100% off)", "di_hidden": "Discount"})

    def test_inclusive_tax_discounts_stay_netted(self):
        source = {**invoice(), "total_excluding_tax": 6198, "total": 7500}
        item = {**line(), "amount": 10000, "discount_amounts": [{"amount": 2500, "discount": "di_1"}]}
        item["taxes"] = [{**item["taxes"][0], "amount": 1302, "tax_behavior": "inclusive"}]
        values, _ = document_plan(config(), source, source, [item], 6)
        self.assertEqual(len(values["invoice_line_ids"]), 1)


class TaxRuleTests(unittest.TestCase):
    worker = WorkerTests.worker

    def setUp(self):
        WorkerTests.setUp(self)
        self.stripe.lines[0]["taxes"][0]["tax_rate_details"]["tax_rate"] = "txr_per_invoice"

    def test_unknown_tax_rate_id_is_mapped_by_country_and_percentage(self):
        self.settings["tax_rate_map"] = {"NL:21": 5}
        self.assertEqual(self.worker().run()["verified"], 1)
        self.assertEqual(self.odoo.creates, 1)

    def test_unknown_tax_rate_without_matching_rule_fails_without_writing(self):
        self.assertEqual(self.worker().run()["failed"], 1)
        self.settings["tax_rate_map"] = {"NL:9": 5}
        self.assertEqual(self.worker().run()["failed"], 1)
        self.assertEqual(self.odoo.creates, 0)

    def test_fully_discounted_invoice_needs_no_tax_treatment(self):
        self.settings["no_tax"] = {}
        self.stripe.invoices[0].update(total=0, total_excluding_tax=0, amount_paid=0)
        self.stripe.lines[0].update(taxes=[], discount_amounts=[{"amount": 10000}])
        self.assertEqual(self.worker().run()["verified"], 1)

    def test_untaxed_invoice_with_value_still_needs_a_treatment(self):
        self.settings["no_tax"] = {}
        self.stripe.lines[0]["taxes"] = []
        self.assertEqual(self.worker().run()["failed"], 1)


class MatchingTests(unittest.TestCase):
    worker = WorkerTests.worker

    def setUp(self):
        WorkerTests.setUp(self)
        self.settings["partner_map"] = {}
        self.stripe.invoices[0].update(
            customer_email="billing@example.test",
            customer_tax_ids=[{"type": "eu_vat", "value": "NL123456789B01"}],
        )
        self.odoo.records["res.partner", 6] = {
            "id": 6,
            "name": "Example",
            "email": "billing@example.test",
            "vat": "NL123456789B01",
            "company_id": 1,
            "active": True,
        }

    def external_invoice(self):
        values, _ = document_plan(self.settings, invoice(), invoice(), [line()], 6)
        target = self.odoo.create("account.move", values)
        self.odoo.records["account.move", target].update(
            ref=False,
            name=f"INV/2026/{target}",
            state="posted",
        )
        return target

    def test_dry_run_links_existing_contacts_and_documents_without_remote_changes(self):
        target = self.external_invoice()
        before = copy.deepcopy(self.odoo.records)
        result = self.worker(False).run()
        self.assertEqual(result, {"contacts_linked": 1, "linked": 1})
        self.assertEqual(self.odoo.records, before)
        self.assertEqual(self.odoo.creates, 1)
        self.assertEqual(self.odoo.actions, [])
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM operations").fetchone()[0], 0)
        self.assertEqual(self.ledger.db.execute("SELECT target_id FROM document_links").fetchone()[0], target)
        self.assertEqual(self.ledger.db.execute("SELECT target_id FROM contact_links").fetchone()[0], 6)

    def test_apply_replays_local_links_without_creating_posting_or_rewriting_records(self):
        target = self.external_invoice()
        self.odoo.records["account.move", target]["state"] = "draft"
        self.settings["post_invoices"] = True
        before = copy.deepcopy(self.odoo.records)
        self.worker(False).run()
        self.assertEqual(self.worker().run()["linked"], 1)
        self.assertEqual(self.worker().run()["linked"], 1)
        self.assertEqual(self.odoo.records, before)
        self.assertEqual(self.odoo.creates, 1)
        self.assertEqual(self.odoo.actions, [])

    def test_restored_snapshot_reuses_contact_and_invoice_links(self):
        self.external_invoice()
        self.worker(False).run()
        backup = Path(self.temp.name) / "restored.sqlite3"
        self.assertEqual(
            main(
                [
                    "backup",
                    "--state",
                    str(Path(self.temp.name) / "state.sqlite3"),
                    "--output",
                    str(backup),
                ]
            ),
            0,
        )
        with closing(Ledger(backup)) as restored:
            result = Worker(self.settings, self.stripe, self.odoo, restored, apply=True).run()
        self.assertEqual(result, {"contacts_linked": 1, "linked": 1})
        self.assertEqual(self.odoo.creates, 1)
        self.assertEqual(self.odoo.actions, [])

    def test_manual_collapsed_quantities_and_different_descriptions_can_match(self):
        target = self.external_invoice()
        self.stripe.lines[0]["quantity"] = 2
        self.odoo.records["account.move.line", target * 100]["name"] = "Manual description"
        self.assertEqual(self.worker(False).run()["linked"], 1)

    def test_duplicate_candidates_require_an_explicit_document_map(self):
        self.external_invoice()
        target = self.external_invoice()
        self.assertEqual(self.worker().run()["failed"], 1)
        self.assertEqual(self.odoo.creates, 2)
        self.settings["document_map"] = {"in_test": target}
        self.assertEqual(self.worker(False).run()["linked"], 1)

    def test_explicit_document_mapping_still_requires_financial_verification(self):
        target = self.external_invoice()
        self.settings["document_map"] = {"in_test": target}
        self.odoo.records["account.move.line", target * 100]["account_id"] = 99
        self.assertEqual(self.worker().run()["failed"], 1)
        self.assertEqual(self.odoo.creates, 1)

    def test_incompatible_candidates_block_creation_instead_of_falling_back(self):
        target = self.external_invoice()
        mutations = [
            ("account.move", target, "state", "cancel"),
            ("account.move", target, "journal_id", 99),
            ("account.move", target, "ref", "stripe:other:test:invoice:in_other"),
            ("account.move", target, "_tax", 20),
            ("account.move.line", target * 100, "account_id", 99),
            ("account.move.line", target * 100, "tax_ids", [99]),
            ("account.move.line", target * 100, "price_unit", 90),
        ]
        for model, identifier, field, value in mutations:
            with self.subTest(field=field):
                record = self.odoo.records[model, identifier]
                previous = record.get(field)
                record[field] = value
                self.assertEqual(self.worker().run()["failed"], 1)
                self.assertEqual(self.odoo.creates, 1)
                self.assertEqual(self.odoo.actions, [])
                record[field] = previous

    def test_other_company_invoice_is_not_adopted(self):
        target = self.external_invoice()
        self.odoo.records["account.move", target]["company_id"] = 2
        result = self.worker().run()
        self.assertEqual(result["verified"], 1)
        self.assertEqual(self.odoo.creates, 2)
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM document_links").fetchone()[0], 0)

    def test_two_source_invoices_cannot_claim_one_existing_document(self):
        self.external_invoice()
        self.stripe.invoices.append({**self.stripe.invoices[0], "id": "in_other"})
        result = self.worker().run()
        self.assertEqual(result["linked"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(self.odoo.creates, 1)

    def test_linked_record_changes_or_deletion_never_trigger_a_new_create(self):
        target = self.external_invoice()
        self.worker(False).run()
        self.odoo.records["account.move.line", target * 100]["account_id"] = 99
        self.assertEqual(self.worker().run()["failed"], 1)
        del self.odoo.records["account.move", target]
        self.assertEqual(self.worker().run()["failed"], 1)
        self.assertEqual(self.odoo.creates, 1)

    def test_remapping_an_existing_link_is_rejected(self):
        self.external_invoice()
        self.worker(False).run()
        self.settings["document_map"] = {"in_test": 999}
        self.assertEqual(self.worker().run()["failed"], 1)
        self.assertEqual(self.odoo.creates, 1)

    def test_unknown_write_intent_cannot_be_replaced_with_a_migration_match(self):
        self.external_invoice()
        ref = "stripe:acct_test:test:invoice:in_test"
        self.ledger.put(ref, "unknown-fingerprint", "unknown")
        self.assertEqual(self.worker().run()["failed"], 1)
        self.assertEqual(self.ledger.get(ref)["status"], "unknown")
        self.assertEqual(self.odoo.creates, 1)

    def test_adopted_invoices_are_not_automatically_settled_or_cancelled(self):
        self.external_invoice()
        self.worker(False).run()
        self.settings["sync_payments"] = True
        with self.assertRaisesRegex(SyncError, "settlements_require_review"):
            self.worker().invoice(self.stripe.invoices[0])
        self.settings["sync_payments"] = False
        self.stripe.invoices[0]["status"] = "void"
        self.assertEqual(self.worker().run()["failed"], 1)
        self.assertEqual(self.odoo.actions, [])

    def test_adopted_credit_is_not_automatically_reconciled(self):
        self.ledger.link("document", "stripe:acct_test:test:credit:cn_test", 12, "hash")
        with self.assertRaisesRegex(SyncError, "linked_credit_settlement"):
            self.worker().reconcile_credit(
                {"id": "cn_test", "total": 12100, "pre_payment_amount": 12100}, 12, 11
            )
        self.assertEqual(self.odoo.actions, [])

    def test_vat_and_name_match_reuses_contact_with_a_different_email(self):
        self.odoo.records["res.partner", 6]["email"] = "old@example.test"
        self.assertEqual(self.worker().run()["contacts_linked"], 1)
        self.assertEqual(self.odoo.creates, 1)  # Invoice only.

    def test_name_and_email_match_is_case_insensitive(self):
        self.stripe.invoices[0]["customer_tax_ids"] = []
        self.odoo.records["res.partner", 6].update(name="EXAMPLE", email="BILLING@example.test")
        self.assertEqual(self.worker().run()["contacts_linked"], 1)
        self.assertEqual(self.odoo.creates, 1)

    def test_shared_email_without_matching_name_does_not_merge_or_create(self):
        self.odoo.records["res.partner", 6]["name"] = "Different business"
        self.assertEqual(self.worker().run()["failed"], 1)
        self.assertEqual(self.odoo.creates, 0)

    def test_nameless_customer_links_a_unique_mailbox_match(self):
        self.stripe.invoices[0].update(customer_name=None, customer_tax_ids=[])
        self.odoo.records["res.partner", 6]["vat"] = False
        self.assertEqual(self.worker().run()["contacts_linked"], 1)
        self.assertEqual(self.odoo.creates, 1)

    def test_nameless_customer_with_ambiguous_mailbox_match_is_reviewed(self):
        self.stripe.invoices[0].update(customer_name=None, customer_tax_ids=[])
        self.odoo.records["res.partner", 7] = {**self.odoo.records["res.partner", 6], "id": 7}
        self.assertEqual(self.worker().run()["failed"], 1)
        self.assertEqual(self.odoo.creates, 0)

    def test_conflicting_vat_numbers_do_not_merge_or_create(self):
        self.odoo.records["res.partner", 6]["vat"] = "NL999999999B01"
        self.assertEqual(self.worker().run()["failed"], 1)
        self.assertEqual(self.odoo.creates, 0)

    def test_formatted_vat_with_changed_name_is_reviewed_instead_of_duplicated(self):
        self.odoo.records["res.partner", 6].update(
            name="Example Limited",
            email=False,
            vat="NL 123.456.789 B01",
        )
        self.assertEqual(self.worker().run()["failed"], 1)
        self.assertEqual(self.odoo.creates, 0)

    def test_unknown_contact_create_cannot_be_replaced_by_a_migration_match(self):
        self.ledger.put("stripe:acct_test:test:customer:cus_test", "unknown", "unknown")
        self.assertEqual(self.worker().run()["failed"], 1)
        self.assertEqual(self.odoo.creates, 0)

    def test_name_only_match_requires_review_instead_of_creating_a_contact(self):
        self.odoo.records["res.partner", 6].update(email="other@example.test", vat=False)
        self.assertEqual(self.worker().run()["failed"], 1)
        self.assertEqual(self.odoo.creates, 0)

    def test_ambiguous_and_archived_contacts_are_not_duplicated(self):
        self.odoo.records["res.partner", 7] = {**self.odoo.records["res.partner", 6], "id": 7}
        self.assertEqual(self.worker().run()["failed"], 1)
        del self.odoo.records["res.partner", 7]
        self.odoo.records["res.partner", 6]["active"] = False
        self.assertEqual(self.worker().run()["failed"], 1)
        self.assertEqual(self.odoo.creates, 0)

    def test_linked_contact_changes_and_deleted_contact_do_not_create_replacements(self):
        self.worker(False).run()
        self.stripe.invoices[0]["customer_name"] = "Changed name"
        self.assertEqual(self.worker().run()["failed"], 1)
        self.stripe.invoices[0]["customer_name"] = "Example"
        del self.odoo.records["res.partner", 6]
        self.assertEqual(self.worker().run()["failed"], 1)
        self.assertEqual(self.odoo.creates, 0)

    def test_new_contact_is_created_only_when_no_possible_existing_contact_is_found(self):
        self.odoo.records.clear()
        result = self.worker().run()
        self.assertEqual(result["verified"], 1)
        self.assertEqual(self.odoo.creates, 2)  # One contact, one invoice.


class HttpTests(unittest.TestCase):
    def test_reads_retry_and_writes_do_not(self):
        for safe, attempts in ((True, 3), (False, 1)):
            opener = unittest.mock.Mock()
            opener.open.side_effect = urllib.error.HTTPError(
                "https://example.test", 503, "unavailable", {}, None
            )
            with self.assertRaises(UnknownOutcome):
                Http(opener, sleep=lambda _: None).request("https://example.test", {}, {}, safe=safe)
            self.assertEqual(opener.open.call_count, attempts)

    def test_error_bodies_are_not_logged(self):
        opener = unittest.mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError(
            "https://example.test", 403, "PII-SECRET", {}, io.BytesIO(b"PII-SECRET")
        )
        with self.assertRaises(Rejected) as caught:
            Http(opener).request("https://example.test", {})
        self.assertNotIn("PII-SECRET", str(caught.exception))

    def test_redirect_and_untrusted_odoo_urls_are_refused(self):
        for url in (
            "http://digitura.odoo.com",
            "https://user:password@example.test",
            "https://example.test/other",
        ):
            with self.assertRaises(SyncError):
                Odoo(url, None, "key", 1, Http())

    def test_pagination_fetches_every_page_and_checks_modes(self):
        stripe = Stripe("key", "acct_test", False, None)
        with patch.object(
            stripe,
            "get",
            side_effect=[
                {"data": [{"id": "a", "livemode": False}], "has_more": True},
                {"data": [{"id": "b", "livemode": False}], "has_more": False},
            ],
        ) as get:
            self.assertEqual([row["id"] for row in stripe.list("/v1/invoices")], ["a", "b"])
            self.assertEqual(get.call_args.args[1]["starting_after"], "a")
        with patch.object(
            stripe, "get", return_value={"data": [{"id": "a", "livemode": True}], "has_more": False}
        ):
            with self.assertRaisesRegex(SyncError, "mode_mismatch"):
                list(stripe.list("/v1/invoices"))


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("sys.stdout", new=io.StringIO()))
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.temp.name) / "state.sqlite3")
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.ledger.close)
        self.settings = config()
        self.settings.update(payment_journal_id=7, payment_method_line_id=8)
        self.stripe, self.odoo = FakeStripe(), ReceiptOdoo()
        self.allocation = {
            "id": "inpay_test",
            "amount_paid": 6000,
            "payment": {"type": "payment_intent", "payment_intent": "pi_test"},
            "status_transitions": {"paid_at": 1767222000},
        }
        self.stripe.allocations = [self.allocation]
        self.stripe.get = lambda _: {"status": "succeeded", "currency": "eur", "amount_received": 6000}
        self.worker = Worker(self.settings, self.stripe, self.odoo, self.ledger, apply=True)
        self.worker.document(invoice(), invoice(), [line()], 6)
        self.source = invoice()
        self.source["amount_remaining"] = 6100

    def test_partial_receipt_preserves_residual_and_replay_does_not_repay(self):
        self.worker.payments(self.source, 11)
        self.worker.payments(self.source, 11)
        self.assertEqual(self.odoo.records["account.move", 11]["amount_residual"], 61)
        self.assertEqual(self.odoo.wizard["payment_difference_handling"], "open")
        self.assertTrue(self.odoo.wizard["group_payment"])
        self.assertEqual(len([key for key in self.odoo.records if key[0] == "account.payment"]), 1)

    def test_lost_receipt_response_recovers_existing_receipt(self):
        self.odoo.lose_receipt_response = True
        with self.assertRaises(UnknownOutcome):
            self.worker.payments(self.source, 11)
        self.worker.payments(self.source, 11)
        self.assertEqual(self.odoo.records["account.move", 11]["amount_residual"], 61)

    def test_noneditable_wizard_never_creates_receipt(self):
        self.odoo.editable = False
        with self.assertRaises(Rejected):
            self.worker.payments(self.source, 11)
        self.assertNotIn(("account.payment", 91), self.odoo.records)

    def test_shared_payment_is_quarantined_before_receipt(self):
        self.stripe.allocations.append({**self.allocation, "id": "inpay_other"})
        with self.assertRaisesRegex(SyncError, "shared_payment_requires_review"):
            self.worker.payments(self.source, 11)
        self.assertIsNone(self.odoo.wizard)

    def test_dry_run_does_not_create_payment_wizard(self):
        self.worker.apply = False
        self.worker.payments(self.source, 11)
        self.assertIsNone(self.odoo.wizard)


if __name__ == "__main__":
    unittest.main()
