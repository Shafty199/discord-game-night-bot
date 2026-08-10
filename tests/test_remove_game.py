import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from commands.admin import (
    _remove_game_record,
    _resolve_game_selection,
)


class RemoveGameTests(unittest.IsolatedAsyncioTestCase):
    @patch(
        "commands.admin.get_all_game_cache_records",
        new_callable=AsyncMock,
    )
    async def test_autocomplete_id_resolves_exact_game(
        self,
        get_records,
    ):
        expected = {
            "id": 42,
            "name": "Accidental Game",
        }
        get_records.return_value = [
            {"id": 10, "name": "Other Game"},
            expected,
        ]

        result = await _resolve_game_selection("id:42")

        self.assertIs(result, expected)

    @patch(
        "commands.admin.get_game_cache_record",
        new_callable=AsyncMock,
    )
    async def test_typed_name_still_resolves_case_insensitively(
        self,
        get_record,
    ):
        expected = {
            "id": 42,
            "name": "Accidental Game",
        }
        get_record.return_value = expected

        result = await _resolve_game_selection(
            "  accidental game  "
        )

        self.assertIs(result, expected)
        get_record.assert_awaited_once_with(
            name="accidental game"
        )

    @patch(
        "commands.admin.delete_local_game_artwork",
        new_callable=AsyncMock,
    )
    @patch(
        "commands.admin.delete_game_by_name",
        new_callable=AsyncMock,
    )
    @patch(
        "commands.admin.get_game_cache_record",
        new_callable=AsyncMock,
    )
    async def test_removal_cleans_artwork_and_refreshes_spins(
        self,
        get_record,
        delete_game,
        delete_artwork,
    ):
        game = {
            "id": 42,
            "name": "Accidental Game",
        }
        get_record.return_value = dict(game)
        delete_game.return_value = True
        manager = SimpleNamespace(
            invalidate_library=AsyncMock(return_value=40),
        )
        bot = SimpleNamespace(
            prepared_spin_manager=manager
        )

        status = await _remove_game_record(
            bot=bot,
            game_record=game,
        )

        self.assertEqual(status, "removed")
        delete_game.assert_awaited_once_with(
            "Accidental Game"
        )
        delete_artwork.assert_awaited_once_with(42)
        manager.invalidate_library.assert_awaited_once_with()

    @patch(
        "commands.admin.delete_game_by_name",
        new_callable=AsyncMock,
    )
    @patch(
        "commands.admin.get_game_cache_record",
        new_callable=AsyncMock,
    )
    async def test_stale_confirmation_does_not_delete_another_game(
        self,
        get_record,
        delete_game,
    ):
        get_record.return_value = {
            "id": 99,
            "name": "Accidental Game",
        }

        status = await _remove_game_record(
            bot=SimpleNamespace(
                prepared_spin_manager=None
            ),
            game_record={
                "id": 42,
                "name": "Accidental Game",
            },
        )

        self.assertEqual(status, "stale")
        delete_game.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
