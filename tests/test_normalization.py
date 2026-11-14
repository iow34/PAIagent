import io
import unittest
import zipfile
from datetime import UTC, datetime

from pypdf import PdfWriter

from app.integration.validation import ContractError
from app.normalization.adapters import extract
from app.normalization.common import parse_file, structured
from app.normalization.csv_adapter import Column, CSVConfig, rows


def epub_bytes(encrypted=False):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr(
            "META-INF/container.xml",
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="content.opf"/></rootfiles></container>',
        )
        z.writestr(
            "content.opf",
            '<package xmlns="http://www.idpf.org/2007/opf"><manifest><item id="c" href="c.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="c"/></spine></package>',
        )
        z.writestr(
            "c.xhtml",
            '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>Hello book</p><script>secret active code</script></body></html>',
        )
        if encrypted:
            z.writestr("META-INF/encryption.xml", "<encryption/>")
    return output.getvalue()


class NormalizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_and_markdown_isolated(self):
        for extension in ["txt", "md"]:
            self.assertEqual(
                (await parse_file("Привіт".encode(), extension)).units[0].text, "Привіт"
            )
            with self.assertRaises(ContractError):
                await parse_file(b"\xff", extension)

    def test_epub_spine_and_encryption(self):
        result = extract(epub_bytes(), "epub")
        self.assertIn("Hello book", result.units[0].text)
        self.assertNotIn("active code", result.units[0].text)
        self.assertEqual(result.units[0].position["section"], 1)
        with self.assertRaises(ValueError):
            extract(epub_bytes(True), "epub")

    def test_pdf_scanned_and_encrypted(self):
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        output = io.BytesIO()
        writer.write(output)
        with self.assertRaises(ValueError):
            extract(output.getvalue(), "pdf")
        writer.encrypt("secret")
        output = io.BytesIO()
        writer.write(output)
        with self.assertRaises(ValueError):
            extract(output.getvalue(), "pdf")

    def test_pdf_positive_text(self):
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

        writer = PdfWriter()
        page = writer.add_blank_page(width=200, height=200)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject(
                    {NameObject("/F1"): writer._add_object(font)}
                )
            }
        )
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 12 Tf 10 100 Td (Hello PDF) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(stream)
        output = io.BytesIO()
        writer.write(output)
        result = extract(output.getvalue(), "pdf")
        self.assertIn("Hello PDF", result.units[0].text)
        self.assertEqual(result.units[0].position, {"page": 1})

    def test_csv_json_equivalence_and_row_errors(self):
        config = CSVConfig(
            [Column("title", "caption", "string"), Column("count", "n", "integer")],
            "id",
            "version",
            datetime.now(UTC),
        )
        converted = rows(b"id,version,title,count\na,1,Hello,3\nb,2,Bad,no\n", config)
        self.assertEqual(converted[0]["payload"], {"caption": "Hello", "n": 3})
        self.assertEqual(converted[1]["line"], 3)
        self.assertIn("error", converted[1])
        values = structured(
            converted[0]["payload"], [{"path": "/caption", "role": "search_text"}]
        )
        self.assertEqual(
            values,
            structured(
                {"caption": "Hello", "n": 3},
                [{"path": "/caption", "role": "search_text"}],
            ),
        )
        self.assertEqual(values[0][1]["field"], "/caption")
        with self.assertRaises(ContractError):
            rows(b"id,id\na,b", config)

    def test_limits_and_unselected_fields(self):
        with self.assertRaises(ValueError):
            extract(b"x" * (5 * 1024 * 1024 + 1), "txt")
        with self.assertRaises(zipfile.BadZipFile):
            extract(b"not zip", "epub")
        self.assertEqual(structured({"private": "do not index"}, []), [])
