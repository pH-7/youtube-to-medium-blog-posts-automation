import os
import tempfile
import unittest
from pathlib import Path

from book_compiler import collect_chapters, compile_book


class RealWorldBookCompileTest(unittest.TestCase):
    @unittest.skipUnless(
        os.getenv("RUN_REAL_WORLD_TESTS") == "1",
        "Set RUN_REAL_WORLD_TESTS=1 to run real-content integration tests",
    )
    def test_compile_epub_from_real_articles(self) -> None:
        articles_dir = Path("articles")
        self.assertTrue(articles_dir.exists(), "articles directory is missing")

        chapters = collect_chapters(articles_dir)
        self.assertGreaterEqual(
            len(chapters),
            5,
            "Expected at least 5 published articles for a realistic compilation",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            outputs = compile_book(
                title="Real World Compilation Test",
                author="Automation Test",
                source_dir=articles_dir,
                output_dir=Path(tmp_dir),
                language="fr",
                formats=("epub",),
                embed_images=False,
            )

            self.assertIsInstance(outputs, list)
            self.assertGreaterEqual(len(outputs), 1)

            epub_path = Path(outputs[0])
            self.assertEqual(epub_path.suffix, ".epub")
            self.assertTrue(epub_path.exists(), "EPUB file was not generated")
            self.assertGreater(epub_path.stat().st_size, 50_000, "EPUB output looks too small")


if __name__ == "__main__":
    unittest.main()
