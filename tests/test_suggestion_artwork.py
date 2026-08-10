import asyncio
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault(
    "DISCORD_TOKEN",
    "test-token",
)

import settings
from discord.ext import commands
from utils import artwork_cache

settings.DISCORD_TOKEN = "test-token"

with patch.object(commands.Bot, "run"):
    import bot as bot_module


class ArtworkValidationTests(unittest.TestCase):
    def test_truncated_artwork_is_rejected(self):
        temporary_directory = (
            Path.cwd()
            / ".test-tmp"
            / "artwork-validation"
        )
        temporary_directory.mkdir(
            parents=True,
            exist_ok=True,
        )
        valid_path = temporary_directory / "valid.jpg"
        truncated_path = (
            temporary_directory / "truncated.jpg"
        )

        try:
            image = artwork_cache.Image.new(
                "RGB",
                (64, 64),
                "navy",
            )
            image.save(valid_path, format="JPEG")
            image.close()

            valid_bytes = valid_path.read_bytes()
            truncated_path.write_bytes(
                valid_bytes[: len(valid_bytes) // 2]
            )

            self.assertTrue(
                artwork_cache._verify_artwork_file(
                    valid_path
                )
            )
            self.assertFalse(
                artwork_cache._verify_artwork_file(
                    truncated_path
                )
            )

        finally:
            valid_path.unlink(missing_ok=True)
            truncated_path.unlink(missing_ok=True)


class SuggestionArtworkTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_artwork_jobs_run_concurrently_within_limit(self):
        active_jobs = 0
        maximum_active_jobs = 0

        async def prepare_artwork(
            *,
            bot,
            game_record,
            refresh,
        ):
            nonlocal active_jobs
            nonlocal maximum_active_jobs

            active_jobs += 1
            maximum_active_jobs = max(
                maximum_active_jobs,
                active_jobs,
            )

            try:
                await asyncio.sleep(0.02)
                return (
                    "cached"
                    if refresh
                    else "already_cached"
                )

            finally:
                active_jobs -= 1

        game_infos = [
            {"name": f"Game {index}"}
            for index in range(7)
        ]
        artwork_jobs = [
            (
                game_infos[index],
                {
                    "id": index + 1,
                    "name": f"Game {index}",
                },
                index % 2 == 0,
            )
            for index in range(7)
        ]

        with patch.object(
            bot_module,
            "prepare_local_game_artwork",
            side_effect=prepare_artwork,
        ):
            await bot_module._prepare_suggestion_artwork_jobs(
                SimpleNamespace(),
                artwork_jobs,
            )

        self.assertEqual(
            maximum_active_jobs,
            bot_module.SUGGESTION_ARTWORK_CONCURRENCY,
        )
        self.assertTrue(
            all(
                game_info.get("cache_result")
                in {"cached", "already_cached"}
                for game_info in game_infos
            )
        )


if __name__ == "__main__":
    unittest.main()
