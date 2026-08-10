import logging


DISCORD_OPTIONAL_VOICE_WARNINGS = frozenset(
    {
        (
            "PyNaCl is not installed, voice will NOT be "
            "supported"
        ),
        (
            "davey is not installed, voice will NOT be "
            "supported"
        ),
    }
)


class DiscordOptionalVoiceWarningFilter(logging.Filter):
    """Hide optional voice warnings for this text-only bot."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not (
            record.name == "discord.client"
            and record.getMessage()
            in DISCORD_OPTIONAL_VOICE_WARNINGS
        )
