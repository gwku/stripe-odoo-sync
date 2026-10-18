import copy
import io
import sqlite3
import tempfile
import unittest
import urllib.error
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

    def search(self, model, domain, fields, limit=100):
        if model == "account.move.line":
            record = self.records["account.move", domain[0][2]]
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
        return []

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
        with sqlite3.connect(backup) as db:
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
