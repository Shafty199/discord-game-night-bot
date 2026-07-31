import unittest

from utils import epic, store


class StoreURLValidationTests(
    unittest.TestCase
):
    def test_official_store_hosts_are_accepted(self):
        self.assertEqual(
            store.detect_store(
                "https://store.steampowered.com/app/12345/"
            ),
            "Steam",
        )
        self.assertEqual(
            store.detect_store(
                "https://store.epicgames.com/en-US/p/example-game"
            ),
            "Epic Games Store",
        )
        self.assertEqual(
            store.detect_store(
                "https://www.epicgames.com/store/en-US/p/example-game"
            ),
            "Epic Games Store",
        )
        self.assertEqual(
            epic.clean_epic_url(
                "https://store.epicgames.com/en-US/p/example-game"
            ),
            "https://store.epicgames.com/en-US/p/example-game",
        )

    def test_lookalike_store_hosts_are_rejected(self):
        rejected_urls = (
            "https://notsteampowered.com/app/12345/",
            "https://store.steampowered.com.attacker.example/app/12345/",
            "https://notepicgames.com/en-US/p/example-game",
            "https://store.epicgames.com.attacker.example/p/example-game",
        )

        for url in rejected_urls:
            with self.subTest(url=url):
                self.assertIsNone(
                    store.detect_store(
                        url
                    )
                )

        self.assertIsNone(
            epic.clean_epic_url(
                "https://notepicgames.com/en-US/p/example-game"
            )
        )
        self.assertIsNone(
            epic.clean_epic_url(
                "https://store.epicgames.com.attacker.example/p/example-game"
            )
        )


class PlainTextHTMLParserTests(
    unittest.TestCase
):
    def test_html_is_converted_to_plain_text(self):
        self.assertEqual(
            store._html_to_plain_text(
                "<div>Hello &amp; <strong>world</strong></div>"
                "<script>alert('ignored')</script>"
                "<style>body { display: none; }</style>"
            ),
            "Hello & world",
        )

    def test_malformed_and_unclosed_script_content_is_ignored(self):
        self.assertEqual(
            store._html_to_plain_text(
                "<p>Visible text</p>"
                "<script>alert('ignored')</script foo='bar'>"
                "<p>Still visible</p>"
                "<script>everything after this is ignored"
            ),
            "Visible text Still visible",
        )


if __name__ == "__main__":
    unittest.main()
