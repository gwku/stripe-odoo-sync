"""Read-only migration matching. Persist links locally; never rewrite Odoo records."""

import unicodedata
from collections import Counter, defaultdict
from decimal import Decimal

from .core import digest, minor, relation_id, require


def normalized(value):
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def identity(record):
    return {
        "name": normalized(record.get("name")),
        "email": normalized(record.get("email")),
        "vat": "".join(character for character in normalized(record.get("vat")) if character.isalnum()),
    }


class Matcher:
    def __init__(self, config, odoo, ledger):
        self.config, self.odoo, self.ledger = config, odoo, ledger

    def contact(self, ref, values, named=True):
        expected = identity(values)
        fingerprint = digest(expected)
        linked = self.ledger.linked("contact", ref)
        if linked:
            require(linked["fingerprint"] == fingerprint, "customer_snapshot_changed_set_partner_map")
            candidates = [self.odoo.one("res.partner", linked["target_id"], list(values))]
        else:
            operation = self.ledger.get(ref)
            require(
                not operation or operation["status"] == "rejected",
                "existing_contact_matching_blocked_by_prior_operation",
            )
            terms = [[key, "=ilike", expected[key]] for key in ("name", "email") if expected[key]]
            if expected["vat"]:
                # Find formatted VAT numbers too; identity() still requires exact normalized equality.
                terms.append(["vat", "ilike", "%".join(expected["vat"])])
            if not terms:
                return None
            domain = ["|"] * (len(terms) - 1) + terms
            candidates = self.odoo.search(
                "res.partner",
                [
                    "&",
                    "|",
                    ["company_id", "=", False],
                    ["company_id", "=", self.config["company_id"]],
                    *domain,
                ],
                ["id", "name", "email", "vat", "company_id", "active"],
                limit=101,
                context={"active_test": False},
            )
            require(len(candidates) < 101, "too_many_contact_candidates_use_partner_map")
        matches, possible = [], []
        for candidate in candidates:
            actual = identity(candidate)
            shared = {key for key in expected if expected[key] and expected[key] == actual[key]}
            if not shared:
                continue
            possible.append(candidate)
            company = relation_id(candidate["company_id"])
            # A conflicting VAT number must never be overridden by a shared mailbox.
            vat_conflict = expected["vat"] and actual["vat"] and expected["vat"] != actual["vat"]
            # Stripe customers without a name (common for invoices created by a product) can only be
            # matched by mailbox; ambiguity below still demands an explicit partner_map entry.
            identified = ("name" in shared and bool(shared & {"vat", "email"})) or (
                not named and "email" in shared
            )
            if company in (False, self.config["company_id"]) and identified and not vat_conflict:
                matches.append(candidate)
        require(len(matches) <= 1, "ambiguous_existing_contact_use_partner_map")
        require(matches or not possible, "existing_contact_requires_explicit_partner_map")
        require(not linked or matches, "linked_contact_identity_changed")
        if not matches:
            return None
        require(matches[0].get("active", True), "existing_contact_archived_requires_review")
        identifier = matches[0]["id"]
        self.ledger.link("contact", ref, identifier, fingerprint)
        return identifier

    def signature(self, values, expected):
        decimals = expected["decimals"]
        lines = sorted(
            (
                command[2]["account_id"],
                tuple(command[2]["tax_ids"][0][2]),
                minor(
                    Decimal(str(command[2]["quantity"])) * Decimal(str(command[2]["price_unit"])), decimals
                ),
            )
            for command in values["invoice_line_ids"]
        )
        return digest(
            {
                "identity": {
                    key: values.get(key)
                    for key in (
                        "company_id",
                        "journal_id",
                        "move_type",
                        "partner_id",
                        "currency_id",
                        "invoice_date",
                        "reversed_entry_id",
                    )
                },
                "lines": lines,
                "amounts": expected,
            }
        )

    def verify(self, identifier, values, expected):
        actual = self.odoo.one(
            "account.move",
            identifier,
            [
                "id",
                "ref",
                "move_type",
                "company_id",
                "partner_id",
                "currency_id",
                "journal_id",
                "invoice_date",
                "reversed_entry_id",
                "amount_total",
                "amount_untaxed",
                "state",
            ],
        )
        for field in ("move_type", "company_id", "partner_id", "currency_id", "journal_id", "invoice_date"):
            require(relation_id(actual[field]) == values[field], "existing_invoice_identity_mismatch")
        if "reversed_entry_id" in values:
            require(
                relation_id(actual["reversed_entry_id"]) == values["reversed_entry_id"],
                "existing_credit_origin_mismatch",
            )
        require(actual["state"] in ("draft", "posted"), "existing_invoice_cancelled")
        require(
            not (actual.get("ref") or "").startswith("stripe:") or actual["ref"] == values["ref"],
            "existing_invoice_owned_by_other_source",
        )
        decimals = expected["decimals"]
        require(
            minor(actual["amount_total"], decimals) == expected["total"]
            and minor(actual["amount_untaxed"], decimals) == expected["net"],
            "existing_invoice_amount_mismatch",
        )
        lines = self.odoo.search(
            "account.move.line",
            [["move_id", "=", identifier], ["display_type", "=", "product"]],
            ["account_id", "tax_ids", "price_subtotal"],
            limit=1001,
        )
        require(len(lines) < 1001, "too_many_existing_invoice_lines")
        actual_lines = Counter(
            (
                relation_id(line["account_id"]),
                tuple(sorted(line["tax_ids"])),
                minor(line["price_subtotal"], decimals),
            )
            for line in lines
        )
        expected_lines = Counter(
            (
                command[2]["account_id"],
                tuple(command[2]["tax_ids"][0][2]),
                minor(
                    Decimal(str(command[2]["quantity"])) * Decimal(str(command[2]["price_unit"])), decimals
                ),
            )
            for command in values["invoice_line_ids"]
        )
        require(actual_lines == expected_lines, "existing_invoice_line_mapping_mismatch")
        tax_lines = self.odoo.search(
            "account.move.line",
            [["move_id", "=", identifier], ["display_type", "=", "tax"]],
            ["tax_line_id", "amount_currency"],
            limit=1001,
        )
        require(len(tax_lines) < 1001, "too_many_existing_tax_lines")
        taxes = defaultdict(int)
        sign = 1 if values["move_type"] == "out_refund" else -1
        for line in tax_lines:
            taxes[relation_id(line["tax_line_id"])] += sign * minor(line["amount_currency"], decimals)
        require(
            {key: value for key, value in taxes.items() if value}
            == {key: value for key, value in expected["taxes"].items() if value},
            "existing_invoice_tax_mismatch",
        )

    def document(self, source, values, expected):
        ref = values["ref"]
        linked = self.ledger.linked("document", ref)
        fingerprint = self.signature(values, expected)
        mapped = self.config.get("document_map", {}).get(source["id"])
        if linked:
            require(not mapped or mapped == linked["target_id"], "existing_link_target_changed")
            require(linked["fingerprint"] == fingerprint, "existing_link_payload_changed")
            identifier = linked["target_id"]
        else:
            operation = self.ledger.get(ref)
            require(
                not operation or operation["status"] == "rejected",
                "existing_invoice_matching_blocked_by_prior_operation",
            )
            if mapped:
                require(type(mapped) is int and mapped > 0, "invalid_document_mapping")
                identifier = mapped
            else:
                if values["partner_id"] < 0:
                    return None
                candidates = self.odoo.search(
                    "account.move",
                    [
                        [field, "=", values[field]]
                        for field in ("company_id", "partner_id", "move_type", "currency_id", "invoice_date")
                    ],
                    ["id", "amount_total", "amount_untaxed"],
                    limit=101,
                )
                require(len(candidates) < 101, "too_many_existing_invoice_candidates")
                matches = [
                    item
                    for item in candidates
                    if minor(item["amount_total"], expected["decimals"]) == expected["total"]
                    and minor(item["amount_untaxed"], expected["decimals"]) == expected["net"]
                ]
                require(len(matches) <= 1, "ambiguous_existing_invoice_use_document_map")
                if not matches:
                    return None
                identifier = matches[0]["id"]
        self.verify(identifier, values, expected)
        self.ledger.link("document", ref, identifier, fingerprint)
        return identifier
