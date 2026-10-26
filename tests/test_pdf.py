import base64
import io
import unittest
import urllib.error
from unittest.mock import Mock, patch

import test_sync as fixtures

from stripe_odoo.core import Http, SyncError
from stripe_odoo.pdf import MAX_PDF_BYTES, download_pdf


def pdf(number="EX-1", metadata="first"):
    return f"%PDF-1.4\n% fixture {number} {metadata}\n%%EOF".encode()


class PDFDownloadTests(unittest.TestCase):
    def test_signed_stripe_storage_redirect_is_allowed_without_api_credentials(self):
        opener = Mock()
        opener.open.side_effect = [
            urllib.error.HTTPError(
                "https://pay.stripe.com/pdf",
                302,
                "redirect",
                {
                    "Location": "https://stripe-upload-api.s3.us-west-1.amazonaws.com/document.pdf?signature=test"
                },
                None,
            ),
            io.BytesIO(pdf()),
        ]
        self.assertTrue(download_pdf(Http(opener), "https://pay.stripe.com/pdf").startswith(b"%PDF-"))
        for call in opener.open.call_args_list:
            self.assertFalse(call.args[0].has_header("Authorization"))

    def test_foreign_redirect_and_non_https_sources_are_refused(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError(
            "https://pay.stripe.com/pdf", 302, "redirect", {"Location": "https://evil.test/pdf"}, None
        )
        with self.assertRaisesRegex(SyncError, "untrusted"):
            download_pdf(Http(opener), "https://pay.stripe.com/pdf")
        self.assertEqual(opener.open.call_count, 1)
        with self.assertRaisesRegex(SyncError, "untrusted"):
            download_pdf(Http(opener), "http://pay.stripe.com/pdf")

    def test_html_and_oversized_downloads_are_refused(self):
        for data in (b"<html>login</html>", b"%PDF-" + b"x" * MAX_PDF_BYTES):
            with self.subTest(size=len(data)):
                opener = Mock()
                opener.open.return_value = io.BytesIO(data)
                with self.assertRaises(SyncError):
                    download_pdf(Http(opener), "https://pay.stripe.com/pdf")


class PDFStoreTests(unittest.TestCase):
    worker = fixtures.WorkerTests.worker

    def setUp(self):
        fixtures.WorkerTests.setUp(self)
        self.settings["sync_pdfs"] = True
        self.stripe.pdf = lambda source: pdf(source["number"])

    def test_original_pdf_is_uploaded_once_and_verified_on_replay(self):
        result = self.worker().run()
        self.assertEqual(result["pdfs_saved"], 1)
        self.assertEqual(self.odoo.creates, 2)  # Invoice plus original PDF.
        result = self.worker().run()
        self.assertEqual(result["pdfs_reused"], 1)
        self.assertEqual(self.odoo.creates, 2)
        attachment = self.odoo.records["ir.attachment", 12]
        self.assertEqual(base64.b64decode(attachment["raw"]), pdf())
        self.assertEqual(attachment["res_id"], 11)
        self.assertFalse(attachment["public"])

    def test_existing_manual_pdf_with_different_metadata_is_reused_without_overwriting(self):
        fixtures.MatchingTests.setUp(self)
        self.settings["sync_pdfs"] = True
        self.stripe.pdf = lambda source: pdf(source["number"])
        target = fixtures.MatchingTests.external_invoice(self)
        existing = self.odoo.create(
            "ir.attachment",
            {
                "name": "Invoice-EX-1.pdf",
                "description": False,
                "company_id": 1,
                "res_model": "account.move",
                "res_id": target,
                "type": "binary",
                "mimetype": "application/pdf",
                "public": False,
                "raw": base64.b64encode(pdf(metadata="manual-original")).decode(),
            },
        )
        before = dict(self.odoo.records["ir.attachment", existing])
        self.assertEqual(self.worker().run()["pdfs_reused"], 1)
        self.assertEqual(self.odoo.creates, 2)
        self.assertEqual(self.odoo.records["ir.attachment", existing], before)

    def test_dry_run_validates_pdf_without_uploading_or_recording_write_intents(self):
        self.assertEqual(self.worker(False).run()["pdfs_previewed"], 1)
        self.assertEqual(self.odoo.creates, 0)
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM operations").fetchone()[0], 0)

    def test_invalid_source_pdf_stops_before_contact_or_invoice_creation(self):
        self.stripe.pdf = lambda source: b"<html>expired link</html>"
        self.assertEqual(self.worker().run()["failed"], 1)
        self.assertEqual(self.odoo.creates, 0)

    def test_lost_pdf_create_response_recovers_without_another_attachment(self):
        original = self.odoo.create

        def create(model, values):
            if model == "ir.attachment":
                self.odoo.lose_response = True
            return original(model, values)

        with patch.object(self.odoo, "create", create):
            self.assertEqual(self.worker().run()["failed"], 1)
        self.assertEqual(self.worker().run()["pdfs_reused"], 1)
        self.assertEqual(self.odoo.creates, 2)

    def test_deleted_or_changed_saved_pdf_does_not_create_a_replacement(self):
        self.worker().run()
        self.odoo.records["ir.attachment", 12]["raw"] = base64.b64encode(pdf("EX-2")).decode()
        self.assertEqual(self.worker().run()["failed"], 1)
        del self.odoo.records["ir.attachment", 12]
        self.assertEqual(self.worker().run()["failed"], 1)
        self.assertEqual(self.odoo.creates, 2)
