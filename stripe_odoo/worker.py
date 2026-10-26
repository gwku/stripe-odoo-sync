from __future__ import annotations

import datetime as dt
import time
from collections import defaultdict

from .core import Rejected, SyncError, date_of, digest, log, minor, object_id, relation_id, require
from .matching import Matcher
from .pdf import PDFStore, validate_pdf
from .planning import document_plan, reference


class Worker:
    def __init__(self, config, stripe, odoo, ledger, apply=False):
        self.config, self.stripe, self.odoo, self.ledger, self.apply = config, stripe, odoo, ledger, apply
        self.counts = defaultdict(int)

    def preflight(self):
        self.stripe.check()
        config = self.config
        self.odoo.one("res.company", config["company_id"], ["id"])
        journal = self.odoo.one("account.journal", config["sales_journal_id"], ["company_id", "type"])
        require(
            relation_id(journal["company_id"]) == config["company_id"] and journal["type"] == "sale",
            "sales_journal_company_or_type_mismatch",
        )
        accounts = {config["income_account_id"], *config.get("income_account_map", {}).values()}
        for identifier in accounts:
            account = self.odoo.one("account.account", identifier, ["company_ids", "account_type", "active"])
            require(
                config["company_id"] in account["company_ids"]
                and account["active"]
                and account["account_type"] in ("income", "income_other"),
                "invalid_income_account",
            )
        zero_taxes = {identifier for ids in config["no_tax"].values() for identifier in ids}
        for identifier in {*config["tax_map"].values(), *zero_taxes}:
            tax = self.odoo.one(
                "account.tax",
                identifier,
                [
                    "company_id",
                    "type_tax_use",
                    "active",
                    "price_include",
                    "amount_type",
                    "amount",
                    "include_base_amount",
                    "tax_exigibility",
                ],
            )
            require(
                relation_id(tax["company_id"]) == config["company_id"]
                and tax["active"]
                and tax["type_tax_use"] == "sale"
                and not tax["price_include"]
                and tax["amount_type"] == "percent"
                and not tax["include_base_amount"]
                and tax["tax_exigibility"] == "on_invoice",
                "unsupported_odoo_tax_configuration",
            )
            if identifier in zero_taxes:
                require(tax["amount"] == 0, "no_tax_mapping_must_have_zero_rate")
        for name, mapping in config["currencies"].items():
            currency = self.odoo.one("res.currency", mapping["odoo_id"], ["name", "decimal_places", "active"])
            require(
                currency["name"] == name
                and currency["active"]
                and currency["decimal_places"] == mapping["decimals"],
                "currency_configuration_mismatch",
            )
        if config.get("sync_payments"):
            journal = self.odoo.one("account.journal", config["payment_journal_id"], ["company_id", "type"])
            require(
                relation_id(journal["company_id"]) == config["company_id"] and journal["type"] == "bank",
                "invalid_payment_journal",
            )
            line = self.odoo.one(
                "account.payment.method.line",
                config["payment_method_line_id"],
                ["journal_id", "payment_method_id", "payment_account_id"],
            )
            method = self.odoo.one(
                "account.payment.method", relation_id(line["payment_method_id"]), ["code", "payment_type"]
            )
            require(
                relation_id(line["journal_id"]) == config["payment_journal_id"]
                and method["code"] == "manual"
                and method["payment_type"] == "inbound"
                and bool(line["payment_account_id"]),
                "manual_receipt_with_outstanding_account_required",
            )

    def find(self, model, ref, field="ref"):
        return self.odoo.find(model, ref, field)

    def partner(self, invoice):
        source_id = object_id(invoice.get("customer"))
        require(bool(source_id), "stripe_customer_required")
        mapped = self.config.get("partner_map", {}).get(source_id)
        ref = reference(self.config, "customer", source_id)
        if mapped:
            linked = self.ledger.linked("contact", ref)
            require(not linked or linked["target_id"] == mapped, "existing_link_target_changed")
            partner = self.odoo.one("res.partner", mapped, ["company_id"])
            require(
                relation_id(partner["company_id"]) in (False, self.config["company_id"]),
                "partner_company_mismatch",
            )
            return mapped
        address = invoice.get("customer_address") or {}
        values = {
            "name": invoice.get("customer_name") or source_id,
            "ref": ref,
            "company_id": self.config["company_id"],
            "customer_rank": 1,
            "email": invoice.get("customer_email") or False,
            "street": address.get("line1") or False,
            "street2": address.get("line2") or False,
            "city": address.get("city") or False,
            "zip": address.get("postal_code") or False,
        }
        if address.get("country"):
            countries = self.odoo.search("res.country", [["code", "=", address["country"]]], ["id"], limit=2)
            require(len(countries) == 1, "customer_country_not_found")
            values["country_id"] = countries[0]["id"]
        vats = [item["value"] for item in invoice.get("customer_tax_ids", []) if item["type"] == "eu_vat"]
        require(len(vats) <= 1, "multiple_customer_vat_ids_require_review")
        if vats:
            values["vat"] = vats[0]
        existing = self.find("res.partner", ref)
        linked = self.ledger.linked("contact", ref)
        require(not existing or not linked or existing == linked["target_id"], "existing_link_target_changed")
        if not existing:
            matched = Matcher(self.config, self.odoo, self.ledger).contact(
                ref, values, named=bool(invoice.get("customer_name"))
            )
            if matched:
                self.counts["contacts_linked"] += 1
                log("existing_contact_linked", source_id=source_id, target_id=matched)
                return matched
        if not self.apply:
            return existing or -1
        # Freeze the first imported contact snapshot. Later snapshot changes require an explicit partner_map.
        fingerprint = digest(values)

        def verify(identifier):
            actual = self.odoo.one("res.partner", identifier, list(values))
            for key, value in values.items():
                if key != "customer_rank":
                    require(relation_id(actual[key]) == value, "customer_snapshot_changed_set_partner_map")

        return self.ledger.ensure(
            ref, fingerprint, lambda: existing, lambda: self.odoo.create("res.partner", values), verify
        )

    def verify_document(self, identifier, values, expected):
        actual = self.odoo.one(
            "account.move",
            identifier,
            [
                "ref",
                "move_type",
                "company_id",
                "partner_id",
                "currency_id",
                "journal_id",
                "invoice_date",
                "invoice_date_due",
                "reversed_entry_id",
                "amount_total",
                "amount_untaxed",
                "state",
                "invoice_line_ids",
            ],
        )
        for field in (
            "ref",
            "move_type",
            "company_id",
            "partner_id",
            "currency_id",
            "journal_id",
            "invoice_date",
        ):
            require(relation_id(actual[field]) == values[field], "odoo_document_identity_mismatch")
        for field in ("invoice_date_due", "reversed_entry_id"):
            if field in values:
                require(
                    relation_id(actual[field]) == values[field], "odoo_document_origin_or_due_date_mismatch"
                )
        require(actual["state"] in ("draft", "posted"), "odoo_document_cancelled")
        require(
            len(actual["invoice_line_ids"]) == len(values["invoice_line_ids"]),
            "odoo_invoice_line_count_mismatch",
        )
        for line_id, command in zip(actual["invoice_line_ids"], values["invoice_line_ids"], strict=True):
            line = self.odoo.one(
                "account.move.line", line_id, ["name", "quantity", "price_unit", "account_id", "tax_ids"]
            )
            planned = command[2]
            require(
                line["name"] == planned["name"]
                and relation_id(line["account_id"]) == planned["account_id"]
                and sorted(line["tax_ids"]) == planned["tax_ids"][0][2],
                "odoo_invoice_line_mapping_mismatch",
            )
            from decimal import Decimal

            require(
                all(
                    abs(Decimal(str(line[key])) - Decimal(str(planned[key]))) < Decimal("0.000000001")
                    for key in ("quantity", "price_unit")
                ),
                "odoo_invoice_line_precision_mismatch",
            )
        require(
            minor(actual["amount_total"], expected["decimals"]) == expected["total"]
            and minor(actual["amount_untaxed"], expected["decimals"]) == expected["net"],
            "odoo_document_amount_mismatch",
        )
        taxes = defaultdict(int)
        tax_lines = self.odoo.search(
            "account.move.line",
            [["move_id", "=", identifier], ["display_type", "=", "tax"]],
            ["tax_line_id", "amount_currency"],
            limit=10000,
        )
        require(len(tax_lines) < 10000, "too_many_tax_lines")
        sign = 1 if values["move_type"] == "out_refund" else -1
        for line in tax_lines:
            taxes[relation_id(line["tax_line_id"])] += sign * minor(
                line["amount_currency"], expected["decimals"]
            )
        require(
            {key: value for key, value in taxes.items() if value}
            == {key: value for key, value in expected["taxes"].items() if value},
            "odoo_tax_group_mismatch",
        )

    def action(self, ref, fingerprint, target, complete, perform):
        return self.ledger.ensure(
            ref,
            fingerprint,
            lambda: target if complete() else None,
            lambda: (perform(), target)[1],
            lambda _: require(complete(), "action_not_complete"),
        )

    def document(self, source, invoice, lines, partner, original=None):
        values, expected = document_plan(self.config, source, invoice, lines, partner, original)
        ref = values["ref"]
        # Expiring PDF links are presentation data, not document identity.
        fingerprint = digest(
            {"values": {k: v for k, v in values.items() if k != "narration"}, "expected": expected}
        )
        managed = self.find("account.move", ref)
        linked = self.ledger.linked("document", ref)
        mapped = self.config.get("document_map", {}).get(source["id"])
        require(not managed or not linked or managed == linked["target_id"], "existing_link_target_changed")
        require(not managed or not mapped or managed == mapped, "existing_link_target_changed")
        if linked or not managed:
            existing = Matcher(self.config, self.odoo, self.ledger).document(source, values, expected)
            if existing:
                self.counts["linked"] += 1
                log(
                    "existing_document_linked",
                    source_id=source["id"],
                    target_id=existing,
                    mode="apply" if self.apply else "dry-run",
                )
                return existing
        if not self.apply:
            self.counts["previewed"] += 1
            log(
                "document_preview",
                source_id=source["id"],
                total_minor=expected["total"],
                currency=source["currency"],
            )
            return managed or -1
        identifier = self.ledger.ensure(
            ref,
            fingerprint,
            lambda: self.find("account.move", ref),
            lambda: self.odoo.create("account.move", values),
            lambda identifier: self.verify_document(identifier, values, expected),
        )
        if self.config.get("post_invoices"):
            self.action(
                ref + ":post",
                fingerprint,
                identifier,
                lambda: self.odoo.one("account.move", identifier, ["state"])["state"] == "posted",
                lambda: self.odoo.call("account.move", "action_post", ids=[identifier]),
            )
            self.verify_document(identifier, values, expected)
        self.counts["verified"] += 1
        return identifier

    def void(self, source, kind):
        ref = reference(self.config, kind, source["id"])
        require(not self.ledger.linked("document", ref), "linked_document_void_requires_review")
        identifier = self.find("account.move", ref)
        if not identifier:
            require(not self.ledger.get(ref), "voided_document_result_missing")
            self.counts["skipped"] += 1
            return
        state = self.odoo.one("account.move", identifier, ["state"])["state"]
        if state == "cancel":
            return
        require(state == "draft", "posted_source_void_requires_accountant_reversal")
        if self.apply:
            self.action(
                ref + ":void",
                digest({"id": identifier}),
                identifier,
                lambda: self.odoo.one("account.move", identifier, ["state"])["state"] == "cancel",
                lambda: self.odoo.call("account.move", "button_cancel", ids=[identifier]),
            )

    def reconcile_credit(self, credit, credit_id, invoice_id):
        require(
            credit.get("pre_payment_amount") == credit["total"]
            and not credit.get("customer_balance_transaction")
            and not credit.get("post_payment_amount"),
            "post_payment_credit_or_customer_balance_requires_review",
        )
        require(
            not self.ledger.linked("document", reference(self.config, "credit", credit["id"])),
            "linked_credit_settlement_requires_review",
        )
        if not self.apply:
            return

        def complete():
            return self.odoo.one("account.move", credit_id, ["amount_residual"])["amount_residual"] == 0

        def perform():
            lines = self.odoo.search(
                "account.move.line",
                [
                    ["move_id", "in", [invoice_id, credit_id]],
                    ["account_id.account_type", "=", "asset_receivable"],
                    ["reconciled", "=", False],
                ],
                ["id"],
                limit=1000,
            )
            require(1 < len(lines) < 1000, "credit_receivable_lines_require_review")
            self.odoo.call("account.move.line", "reconcile", ids=[line["id"] for line in lines])

        self.action(
            reference(self.config, "credit-reconcile", credit["id"]),
            digest([credit_id, invoice_id]),
            credit_id,
            complete,
            perform,
        )

    def payments(self, invoice, move_id):
        require(
            not invoice.get("starting_balance")
            and not invoice.get("amount_overpaid")
            and not invoice.get("amount_paid_off_stripe"),
            "customer_balance_or_external_payment_requires_review",
        )
        payments = list(
            self.stripe.list("/v1/invoice_payments", {"invoice": invoice["id"], "status": "paid"})
        )
        for allocation in payments:
            payment = allocation["payment"]
            require(payment["type"] == "payment_intent", "unsupported_payment_type_requires_review")
            intent_id = object_id(payment["payment_intent"])
            intent = self.stripe.get(f"/v1/payment_intents/{intent_id}")
            require(
                intent["status"] == "succeeded" and intent["currency"] == invoice["currency"],
                "payment_not_settled",
            )
            amount = allocation["amount_paid"]
            require(
                type(amount) is int and amount > 0 and intent["amount_received"] == amount,
                "payment_amount_or_shared_allocation_requires_review",
            )
            shared = list(
                self.stripe.list(
                    "/v1/invoice_payments",
                    {
                        "payment[type]": "payment_intent",
                        "payment[payment_intent]": intent_id,
                        "status": "paid",
                    },
                )
            )
            require(
                len(shared) == 1 and shared[0]["id"] == allocation["id"], "shared_payment_requires_review"
            )
            ref = reference(self.config, "receipt", allocation["id"])
            currency = self.config["currencies"][invoice["currency"].upper()]
            expected = {
                "move_id": move_id,
                "amount": amount,
                "currency_id": currency["odoo_id"],
                "journal_id": self.config["payment_journal_id"],
                "intent": intent_id,
            }

            def verify(identifier, ref=ref, amount=amount, currency=currency):
                actual = self.odoo.one(
                    "account.payment",
                    identifier,
                    ["memo", "amount", "currency_id", "journal_id", "state", "reconciled_invoice_ids"],
                )
                require(
                    actual["memo"] == ref
                    and minor(actual["amount"], currency["decimals"]) == amount
                    and relation_id(actual["currency_id"]) == currency["odoo_id"]
                    and relation_id(actual["journal_id"]) == self.config["payment_journal_id"]
                    and actual["state"] in ("in_process", "paid")
                    and move_id in actual["reconciled_invoice_ids"],
                    "receipt_verification_failed",
                )

            def create(ref=ref, amount=amount, currency=currency, allocation=allocation):
                move = self.odoo.one("account.move", move_id, ["amount_residual"])
                require(
                    minor(move["amount_residual"], currency["decimals"]) >= amount,
                    "payment_exceeds_odoo_residual",
                )
                context = {"active_model": "account.move", "active_ids": [move_id]}
                values = {
                    "journal_id": self.config["payment_journal_id"],
                    "payment_method_line_id": self.config["payment_method_line_id"],
                    "amount": amount / (10 ** currency["decimals"]),
                    "currency_id": currency["odoo_id"],
                    "payment_date": date_of(
                        allocation["status_transitions"]["paid_at"], self.config["timezone"]
                    ),
                    "communication": ref,
                    "payment_difference_handling": "open",
                    "group_payment": True,
                }
                # A transient wizard is harmless until its business action creates the receipt.
                wizard = self.odoo.call(
                    "account.payment.register", "create", vals_list=[values], context=context
                )[0]
                prepared = self.odoo.one(
                    "account.payment.register",
                    wizard,
                    [
                        "can_edit_wizard",
                        "group_payment",
                        "communication",
                        "amount",
                        "currency_id",
                        "journal_id",
                        "payment_method_line_id",
                    ],
                )
                if not (
                    prepared["can_edit_wizard"]
                    and prepared["group_payment"]
                    and prepared["communication"] == ref
                    and minor(prepared["amount"], currency["decimals"]) == amount
                    and relation_id(prepared["currency_id"]) == currency["odoo_id"]
                    and relation_id(prepared["journal_id"]) == self.config["payment_journal_id"]
                    and relation_id(prepared["payment_method_line_id"])
                    == self.config["payment_method_line_id"]
                ):
                    raise Rejected("payment_wizard_values_require_review")
                self.odoo.call(
                    "account.payment.register", "action_create_payments", ids=[wizard], context=context
                )
                result = self.find("account.payment", ref, "memo")
                require(bool(result), "receipt_result_not_found")
                return result

            if self.apply:
                self.ledger.ensure(
                    ref,
                    digest(expected),
                    lambda ref=ref: self.find("account.payment", ref, "memo"),
                    create,
                    verify,
                )
            self.counts["payment_allocations"] += 1
        if self.apply:
            actual = self.odoo.one("account.move", move_id, ["amount_residual"])
            decimals = self.config["currencies"][invoice["currency"].upper()]["decimals"]
            require(
                minor(actual["amount_residual"], decimals) == invoice["amount_remaining"],
                "invoice_residual_requires_review",
            )

    def invoice(self, invoice):
        if invoice["status"] == "draft":
            self.counts["skipped"] += 1
            return
        if invoice["status"] == "void":
            self.void(invoice, "invoice")
            return
        require(invoice["status"] in ("open", "paid", "uncollectible"), "unsupported_invoice_status")
        pdf = self.fetch_pdf(invoice)
        invoice["_lines"] = list(self.stripe.list(f"/v1/invoices/{invoice['id']}/lines"))
        partner = self.partner(invoice)
        move_id = self.document(invoice, invoice, invoice["_lines"], partner)
        if pdf is not None:
            self.attach_pdf(invoice, move_id, pdf)
        require(
            not self.config.get("sync_payments")
            or not self.ledger.linked("document", reference(self.config, "invoice", invoice["id"])),
            "linked_invoice_settlements_require_review",
        )
        for credit in self.stripe.list("/v1/credit_notes", {"invoice": invoice["id"]}):
            if credit["status"] == "void":
                self.void(credit, "credit")
                continue
            require(credit["status"] == "issued", "unsupported_credit_status")
            pdf = self.fetch_pdf(credit)
            lines = list(self.stripe.list(f"/v1/credit_notes/{credit['id']}/lines"))
            credit_id = self.document(credit, invoice, lines, partner, move_id)
            if pdf is not None:
                self.attach_pdf(credit, credit_id, pdf)
            if self.config.get("sync_payments"):
                self.reconcile_credit(credit, credit_id, move_id)
        if self.config.get("sync_payments"):
            self.payments(invoice, move_id)
        if invoice["status"] == "uncollectible":
            log("accountant_review", source_id=invoice["id"], code="bad_debt_policy_required")
            self.counts["review"] += 1

    def attach_pdf(self, source, move_id, data):
        identifier, status = PDFStore(self.config, self.odoo, self.ledger).sync(
            source, move_id, data, self.apply
        )
        self.counts["pdfs_" + status] += 1
        log("source_pdf_" + status, source_id=source["id"], target_id=identifier)

    def fetch_pdf(self, source):
        if not self.config.get("sync_pdfs", True):
            return None
        data = self.stripe.pdf(source)
        validate_pdf(data)
        return data

    def run(self, cutoff=None):
        self.preflight()
        cutoff = cutoff or self.config["start_date"]
        require(cutoff >= self.config["start_date"], "cutoff_precedes_configured_migration_boundary")
        from zoneinfo import ZoneInfo

        start = int(
            dt.datetime.combine(
                dt.date.fromisoformat(cutoff), dt.time(), ZoneInfo(self.config["timezone"])
            ).timestamp()
        )
        boundary = int(time.time())
        # A full bounded scan is deliberate: older finalized invoices can gain payments and credits.
        for invoice in self.stripe.list("/v1/invoices", {"created[gte]": start, "created[lte]": boundary}):
            try:
                self.invoice(self.stripe.get(f"/v1/invoices/{invoice['id']}"))
            except SyncError as error:
                self.counts["failed"] += 1
                log("document_failed", source_id=invoice["id"], code=str(error))
            except (KeyError, TypeError, ValueError, ArithmeticError):
                self.counts["failed"] += 1
                log("document_failed", source_id=invoice["id"], code="unsupported_source_or_target_schema")
        return dict(self.counts)

    def reconcile(self):
        self.preflight()
        from zoneinfo import ZoneInfo

        start = int(
            dt.datetime.combine(
                dt.date.fromisoformat(self.config["start_date"]), dt.time(), ZoneInfo(self.config["timezone"])
            ).timestamp()
        )
        totals = {}
        for item in self.stripe.list(
            "/v1/balance_transactions", {"created[gte]": start, "created[lte]": int(time.time())}
        ):
            key = (item["currency"], item.get("reporting_category") or item["type"])
            row = totals.setdefault(key, {"count": 0, "gross_minor": 0, "fee_minor": 0, "net_minor": 0})
            row["count"] += 1
            row["gross_minor"] += item["amount"]
            row["fee_minor"] += item["fee"]
            row["net_minor"] += item["net"]
        for (currency, category), amounts in sorted(totals.items()):
            log("settlement_summary", currency=currency, category=category, **amounts)
        log("settlement_review", code="fees_payouts_refunds_disputes_are_not_posted_by_this_worker")
