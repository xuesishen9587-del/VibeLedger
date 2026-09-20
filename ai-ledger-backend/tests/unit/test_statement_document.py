import io
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch
import pypdf
from fastapi import HTTPException
from app.services.statement_document import StatementDocumentParser, MAX_BYTES


class StatementDocumentTest(TestCase):
    def test_pdf_references_preserve_occurrences_and_cannot_claim_unique_ids(self):
        from app.services.statement_document import _STATEMENT_TRANSPORT_SCHEMA
        properties=_STATEMENT_TRANSPORT_SCHEMA["properties"]["lines"]["items"]["properties"]
        self.assertIn("provider_reference",properties)
        self.assertNotIn("provider_transaction_id",properties)
        parser=StatementDocumentParser()
        self.assertIsNone(parser.unique_id_namespace)
        line={"kind":"expense","merchant":"Grab* A-9MALQB8WWS7EAV",
            "provider_reference":"A-9MALQB8WWS7EAV","amount":"26.50"}
        parsed={"lines":[line,line,{**line,"amount":"10.00"}],"processed_pages":[1],"expected_line_count":3,"complete":True}
        with patch.object(parser,"extract",return_value=parsed):
            result=parser.parse(self.pdf(),None,{},[])
            self.assertEqual(len(result["lines"]),3)
            self.assertEqual([r["merchant"] for r in result["lines"]],[line["merchant"]]*3)
            self.assertTrue(all("provider_transaction_id" not in r for r in result["lines"]))
            for field in ("provider_transaction_id","provider_id_namespace"):
                parsed["lines"]=[{**line,field:"model-cannot-assert-uniqueness"}]
                with self.assertRaises(HTTPException):
                    parser.parse(self.pdf(),None,{},[])

    def pdf(self,pages=1,password=None):
        writer=pypdf.PdfWriter()
        for _ in range(pages):
            writer.add_blank_page(width=72,height=72)
        if password:
            writer.encrypt(password)
        buffer=io.BytesIO()
        writer.write(buffer)
        return buffer.getvalue()

    def test_success_failure_and_password_paths_remove_temporary_pdf(self):
        real_temp=tempfile.NamedTemporaryFile
        parsed={"lines":[],"processed_pages":[1],"expected_line_count":0,"complete":True}
        with tempfile.TemporaryDirectory() as folder, patch("app.services.statement_document.tempfile.NamedTemporaryFile",side_effect=lambda **kw:real_temp(dir=folder,**kw)):
            parser=StatementDocumentParser()
            with patch.object(parser,"extract",return_value=parsed):
                result=parser.parse(self.pdf(password="private"),"private",{},[])
                self.assertEqual(result["actual_page_count"],1)
                self.assertEqual(list(Path(folder).iterdir()),[])
                for password in (None,"wrong"):
                    with self.assertRaises(HTTPException) as exc:
                        parser.parse(self.pdf(password="private"),password,{},[])
                    self.assertNotIn("private",str(exc.exception.detail))
                    self.assertEqual(list(Path(folder).iterdir()),[])
            with patch.object(parser,"extract",side_effect=RuntimeError("SECRET_PROVIDER_DETAIL")):
                with self.assertRaises(HTTPException) as exc:
                    parser.parse(self.pdf(),None,{},[])
                self.assertNotIn("SECRET_PROVIDER_DETAIL",str(exc.exception.detail))
                self.assertEqual(list(Path(folder).iterdir()),[])

    def test_page_and_byte_limits_prevent_model_calls(self):
        parser=StatementDocumentParser()
        with patch.object(parser,"extract") as model:
            for content in (self.pdf(pages=51),b"%PDF-"+b"0"*MAX_BYTES,b"not PDF"):
                with self.assertRaises(HTTPException):
                    parser.parse(content,None,{},[])
            model.assert_not_called()
