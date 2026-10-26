from __future__ import annotations

import html
import re
from collections import defaultdict
from decimal import Decimal

from .core import date_of, object_id, require


def reference(config, kind, source_id):
    return (
        f"stripe:{config['stripe_account_id']}:{'live' if config['livemode'] else 'test'}:{kind}:{source_id}"
    )


def taxes_for(config, invoice, line):
    taxes = line.get("taxes")
    require(taxes is not None, "stripe_tax_schema_not_supported")
    mapping, expected, inclusive = [], defaultdict(int), 0
    for tax in taxes:
        source = object_id((tax.get("tax_rate_details") or {}).get("tax_rate"))
        require(source in config["tax_map"], "tax_mapping_required")
        target = config["tax_map"][source]
        require(type(target) is int and target > 0, "invalid_tax_mapping")
        require(tax.get("type") in (None, "tax_rate_details"), "unsupported_stripe_tax_type")
        mapping.append(target)
        expected[target] += tax["amount"]
        if tax.get("tax_behavior") == "inclusive":
            inclusive += tax["amount"]
        else:
            require(tax.get("tax_behavior") == "exclusive", "tax_behavior_required")
    if not taxes and invoice.get("total") == 0:
        # A fully discounted invoice carries no tax, whatever the customer's tax status.
        pass
    elif not taxes:
        treatment = invoice.get("customer_tax_exempt", "none")
        require(treatment in config["no_tax"], "no_tax_treatment_mapping_required")
        mapping = config["no_tax"][treatment]
        require(isinstance(mapping, list), "invalid_no_tax_mapping")
        require(treatment == "none" or bool(mapping), "exempt_or_reverse_requires_tax_reporting_mapping")
    require(len(mapping) == len(set(mapping)), "duplicate_tax_mapping_on_line")
    return sorted(mapping), dict(expected), inclusive


def document_plan(config, source, invoice, lines, partner_id, original_move_id=None, discount_labels=None):
    credit = source.get("object") == "credit_note"
    currency = source["currency"].upper()
    require(currency in config["currencies"], "currency_mapping_required")
    currency_config = config["currencies"][currency]
    factor = Decimal(10) ** currency_config["decimals"]
    require(not source.get("amount_shipping"), "shipping_requires_explicit_mapping")
    require(source.get("total_excluding_tax") is not None, "source_net_total_required")
    require(
        not invoice.get("on_behalf_of") and not invoice.get("transfer_data"),
        "connect_invoice_requires_review",
    )
    require(
        not invoice.get("issuer") or invoice["issuer"].get("type") == "self",
        "external_issuer_requires_review",
    )
    commands, expected_taxes, net_total = [], defaultdict(int), 0
    for line in lines:
        require(
            line["currency"].upper() == currency if "currency" in line else credit, "line_currency_mismatch"
        )
        # Non-discount pretax credits (e.g. billing credit grants) need their own accounting policy.
        require(
            all(item.get("type") == "discount" for item in line.get("pretax_credit_amounts", [])),
            "billing_credit_grant_requires_review",
        )
        tax_ids, tax_totals, inclusive = taxes_for(config, invoice, line)
        discount = sum(item["amount"] for item in line.get("discount_amounts", []))
        net = line["amount"] - discount - inclusive
        net_total += net
        for tax_id, amount in tax_totals.items():
            expected_taxes[tax_id] += amount
        quantity = Decimal(str(line.get("quantity_decimal") or line.get("quantity") or 1))
        require(quantity > 0, "invalid_line_quantity")
        pricing = line.get("pricing") or {}
        product = object_id((pricing.get("price_details") or {}).get("product"))
        if credit and line.get("invoice_line_item"):
            original_line = next(
                (item for item in invoice.get("_lines", []) if item["id"] == line["invoice_line_item"]), None
            )
            require(original_line is not None, "original_credit_line_not_found")
            product = object_id(
                ((original_line.get("pricing") or {}).get("price_details") or {}).get("product")
            )
        account_id = config.get("income_account_map", {}).get(product, config["income_account_id"])
        description = line.get("description") or line["id"]
        period = None
        if (line.get("parent") or {}).get("type") == "subscription_item_details":
            # Stripe's API wraps subscription lines as "1 × Name (at €9.00 / month)"; its PDF shows just the name.
            wrapped = re.fullmatch(r"\d+ × (.+) \([^()]*\)", description)
            description = wrapped.group(1) if wrapped else description
            period = line.get("period")
        if period:
            description += f" [{date_of(period['start'], config['timezone'])} {config.get('period_separator', 'to')} {date_of(period['end'], config['timezone'])}]"
        gross = net + discount
        # Show discounts as their own negative lines, like Stripe's invoice. Inclusive taxes stay netted.
        separate = (
            [item for item in line.get("discount_amounts", []) if item["amount"]] if not inclusive else []
        )
        if separate:
            net = gross
        commands.append(
            [
                0,
                0,
                {
                    "name": description,
                    "quantity": float(quantity),
                    "price_unit": float(Decimal(net) / factor / quantity),
                    "account_id": account_id,
                    "tax_ids": [[6, 0, tax_ids]],
                },
            ]
        )
        for item in separate:
            commands.append(
                [
                    0,
                    0,
                    {
                        "name": (discount_labels or {}).get(item.get("discount"), "Discount"),
                        "quantity": 1.0,
                        "price_unit": float(-Decimal(item["amount"]) / factor),
                        "account_id": account_id,
                        "tax_ids": [[6, 0, tax_ids]],
                    },
                ]
            )
    require(bool(commands), "empty_document_requires_review")
    require(net_total == source["total_excluding_tax"], "source_line_net_total_mismatch")
    require(net_total + sum(expected_taxes.values()) == source["total"], "source_line_gross_total_mismatch")
    kind = "credit" if credit else "invoice"
    ref = reference(config, kind, source["id"])
    issued = source.get("effective_at") or (
        source["created"] if credit else (source.get("status_transitions") or {}).get("finalized_at")
    )
    require(bool(issued), "source_issue_date_required")
    links = [("Stripe number", source.get("number") or ""), ("Stripe ID", source["id"])]
    url = source.get("pdf") if credit else source.get("invoice_pdf")
    if url:
        from urllib.parse import urlsplit

        parsed = urlsplit(url)
        require(
            parsed.scheme == "https"
            and parsed.hostname
            and (parsed.hostname == "stripe.com" or parsed.hostname.endswith(".stripe.com")),
            "invalid_stripe_document_url",
        )
    narration = "<p>" + "<br/>".join(f"{key}: {html.escape(value)}" for key, value in links) + "</p>"
    if url:
        narration += f'<p><a href="{html.escape(url, quote=True)}">Original Stripe document</a></p>'
    values = {
        "company_id": config["company_id"],
        "journal_id": config["sales_journal_id"],
        "move_type": "out_refund" if credit else "out_invoice",
        "partner_id": partner_id,
        "currency_id": currency_config["odoo_id"],
        "ref": ref,
        "invoice_date": date_of(issued, config["timezone"]),
        "invoice_line_ids": commands,
        "narration": narration,
        "fiscal_position_id": False,
    }
    if not credit and source.get("due_date"):
        values["invoice_date_due"] = date_of(source["due_date"], config["timezone"])
    if credit:
        require(bool(original_move_id), "original_odoo_invoice_required")
        values["reversed_entry_id"] = original_move_id
    expected = {
        "net": net_total,
        "total": source["total"],
        "taxes": dict(expected_taxes),
        "decimals": currency_config["decimals"],
    }
    return values, expected
