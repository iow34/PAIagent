import unittest

from app.ingestion.chunking import chunk_text


class ChunkTextTests(unittest.TestCase):
    def test_long_paragraph_requests_limit(self) -> None:
        chunks = chunk_text("a" * 2500)

        self.assertEqual([len(chunk) for chunk in chunks], [1000, 1000, 700])

    def test_zero_overlap(self) -> None:
        chunks = chunk_text(
            "abcdefghij",
            max_chars=4,
            overlap=0,
        )

        self.assertEqual(chunks, ["abcd", "efgh", "ij"])

    def test_overlap(self) -> None:
        chunks = chunk_text(
            "abcdefghij",
            max_chars=4,
            overlap=1,
        )

        self.assertEqual(chunks, ["abcd", "defg", "ghij"])

    def test_paragraph_boundary(self) -> None:
        chunks = chunk_text(
            "First paragraph.\n\nSecond paragraph.",
            max_chars=20,
            overlap=0,
        )

        self.assertEqual(
            chunks,
            ["First paragraph.\n\n", "Second paragraph."],
        )

    def test_text_is_preserved_without_overlap(self) -> None:
        text = "First paragraph.\n\nSecond paragraph."

        chunks = chunk_text(
            text,
            max_chars=20,
            overlap=0,
        )

        self.assertEqual("".join(chunks), text)

    def test_whitespace_only_text(self) -> None:
        self.assertEqual(chunk_text(" \n\t"), [])

    def test_invalid_parameters(self) -> None:
        cases = [
            (0, 0),
            (10, -1),
            (10, 10),
            (10, 11),
        ]

        for max_chars, overlap in cases:
            with self.subTest(max_chars=max_chars, overlap=overlap):
                with self.assertRaises(ValueError):
                    chunk_text(
                        "Текст",
                        max_chars=max_chars,
                        overlap=overlap,
                    )


if __name__ == "__main__":
    unittest.main()
