import logging
import unittest

from utils.logging_filters import (
    DiscordOptionalVoiceWarningFilter,
)


class DiscordLoggingFilterTests(unittest.TestCase):
    def setUp(self):
        self.filter = DiscordOptionalVoiceWarningFilter()

    def _record(
        self,
        message: str,
        *,
        logger_name: str = "discord.client",
    ) -> logging.LogRecord:
        return logging.LogRecord(
            logger_name,
            logging.WARNING,
            __file__,
            1,
            message,
            (),
            None,
        )

    def test_optional_voice_warnings_are_hidden(self):
        messages = (
            (
                "PyNaCl is not installed, voice will NOT be "
                "supported"
            ),
            (
                "davey is not installed, voice will NOT be "
                "supported"
            ),
        )

        for message in messages:
            with self.subTest(message=message):
                self.assertFalse(
                    self.filter.filter(
                        self._record(message)
                    )
                )

    def test_other_discord_warnings_remain_visible(self):
        self.assertTrue(
            self.filter.filter(
                self._record(
                    "Discord gateway connection was interrupted"
                )
            )
        )

    def test_other_loggers_are_not_affected(self):
        self.assertTrue(
            self.filter.filter(
                self._record(
                    "PyNaCl is not installed, voice will NOT be "
                    "supported",
                    logger_name="game-night-bot",
                )
            )
        )


if __name__ == "__main__":
    unittest.main()
