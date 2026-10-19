"""Preserve original PDFs on Odoo moves, avoiding repeat uploads."""

import base64
import hashlib
import re
import urllib.error
import urllib.parse
import urllib.request

from .core import SyncError, UnknownOutcome, digest, relation_id, require
from .planning import reference

MAX_PDF_BYTES = 8 * 1024 * 1024


def allowed_url(url, redirect=False):
    parsed = urllib.parse.urlsplit(url or "")
    host = parsed.hostname or ""
    stripe = host == "stripe.com" or host.endswith(".stripe.com")
    storage = bool(re.fullmatch(r"stripe-upload-api\.s3\.[a-z0-9-]+\.amazonaws\.com", host))
    require(
        parsed.scheme == "https"
        and not parsed.username
        and not parsed.password
        and parsed.port in (None, 443)
        and (stripe or redirect and storage),
        "untrusted_stripe_pdf_url",
    )


def download_pdf(http, url):
    require(isinstance(url, str) and bool(url), "source_pdf_not_available")
    for hop in range(5):
        allowed_url(url, redirect=hop > 0)
        for attempt in range(3):
            try:
                # Use the no-redirect opener; validate each hop and never send API credentials.
                request = urllib.request.Request(url, headers={"User-Agent": "stripe-odoo-sync/1.0"})
                with http.opener.open(request, timeout=45) as response:
                    data = response.read(MAX_PDF_BYTES + 1)
                require(len(data) <= MAX_PDF_BYTES, "pdf_too_large")
                require(data.startswith(b"%PDF-"), "response_is_not_pdf")
                return data
            except urllib.error.HTTPError as error:
                if error.code in (301, 302, 303, 307, 308):
                    location = error.headers.get("Location")
                    require(bool(location), "pdf_redirect_without_location")
                    url = urllib.parse.urljoin(url, location)
                    break
                if error.code in (429, 500, 502, 503, 504) and attempt < 2:
                    http.sleep(2**attempt)
                    continue
                raise SyncError(f"pdf_http_{error.code}") from None
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt < 2:
                    http.sleep(2**attempt)
                    continue
                raise UnknownOutcome("pdf_download_failed") from None
    raise SyncError("too_many_pdf_redirects")


def validate_pdf(data):
    require(len(data) <= MAX_PDF_BYTES and data.startswith(b"%PDF-"), "invalid_pdf_document")


class PDFStore:
    def __init__(self, config, odoo, ledger):
        self.config, self.odoo, self.ledger = config, odoo, ledger

    def sync(self, source, move_id, data, apply):
        validate_pdf(data)
        kind = "credit-pdf" if source.get("object") == "credit_note" else "invoice-pdf"
        ref = reference(self.config, kind, source["id"])
        operation = self.ledger.get(ref)

        def read(identifier):
            attachment = self.odoo.one(
                "ir.attachment",
                identifier,
                ["id", "res_model", "res_id", "company_id", "mimetype", "type", "raw", "public"],
            )
            require(
                attachment["res_model"] == "account.move"
                and attachment["res_id"] == move_id
                and relation_id(attachment["company_id"]) in (False, self.config["company_id"])
                and attachment["type"] == "binary"
                and attachment["mimetype"] == "application/pdf"
                and not attachment["public"],
                "pdf_attachment_identity_mismatch",
            )
            try:
                actual = base64.b64decode(attachment["raw"], validate=True)
            except (ValueError, TypeError):
                raise SyncError("invalid_pdf_attachment_data") from None
            validate_pdf(actual)
            return actual

        existing = None
        if operation and operation["target_id"]:
            existing = operation["target_id"]
        elif move_id > 0:
            filename = f"Invoice-{source['number']}.pdf" if kind == "invoice-pdf" else None
            candidates = self.odoo.search(
                "ir.attachment",
                [
                    ["res_model", "=", "account.move"],
                    ["res_id", "=", move_id],
                    ["mimetype", "=", "application/pdf"],
                    "|",
                    ["description", "=", ref],
                    ["name", "=", filename or f"Stripe-{source['id']}.pdf"],
                ],
                ["id"],
                limit=2,
            )
            require(len(candidates) <= 1, "ambiguous_existing_pdf_attachment")
            existing = candidates[0]["id"] if candidates else None
        # Keep the original attachment bytes; Stripe can regenerate PDF metadata between runs.
        expected = read(existing) if existing else data
        fingerprint = digest({"move_id": move_id, "sha256": hashlib.sha256(expected).hexdigest()})
        if not apply:
            return existing, "reused" if existing else "previewed"

        def verify(identifier):
            require(read(identifier) == expected, "pdf_attachment_content_mismatch")

        def create():
            return self.odoo.create(
                "ir.attachment",
                {
                    "name": f"Stripe-{source['id']}.pdf",
                    "description": ref,
                    "type": "binary",
                    "mimetype": "application/pdf",
                    "public": False,
                    "res_model": "account.move",
                    "res_id": move_id,
                    "company_id": self.config["company_id"],
                    "datas": base64.b64encode(data).decode("ascii"),
                },
            )

        identifier = self.ledger.ensure(ref, fingerprint, lambda: existing, create, verify)
        return identifier, "reused" if existing else "saved"
