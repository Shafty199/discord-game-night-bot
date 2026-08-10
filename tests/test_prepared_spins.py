import time
import unittest
from collections import deque
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from ui.animation import create_hosted_spin_embed
from utils.prepared_spins import (
    PREPARED_SPIN_MAX_AGE_SECONDS,
    PREPARED_SPIN_POOL_SIZE,
    PREPARED_SPIN_DURATION_SECONDS,
    PreparedSpin,
    PreparedSpinManager,
    SessionSpinPool,
    _public_object_url,
)


class _AsyncMessageHistory:
    def __init__(self, messages):
        self.messages = tuple(messages)

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for message in self.messages:
            yield message


def _prepared_spin(
    game_id: int,
    *,
    created_at: float | None = None,
) -> PreparedSpin:
    return PreparedSpin(
        winning_game=(
            game_id,
            f"Game {game_id}",
            "https://store.example/game",
            "Steam",
        ),
        wheel_type="multiplayer",
        object_name=(
            f"prepared/multiplayer/{game_id}.gif"
        ),
        image_url=(
            f"https://objects.example/{game_id}.gif"
        ),
        duration_seconds=8.0,
        created_at=(
            time.monotonic()
            if created_at is None
            else created_at
        ),
    )


class PreparedSpinTests(unittest.TestCase):
    def test_public_object_url_preserves_prefix_and_encodes_name(self):
        url = _public_object_url(
            namespace="example namespace",
            bucket="spin bucket",
            region="us-phoenix-1",
            object_name=(
                "prepared/multiplayer/Game Night #1.gif"
            ),
        )

        self.assertEqual(
            url,
            (
                "https://objectstorage.us-phoenix-1."
                "oraclecloud.com/n/example%20namespace/"
                "b/spin%20bucket/o/prepared/multiplayer/"
                "Game%20Night%20%231.gif"
            ),
        )

    def test_hosted_embed_uses_discord_attachment_url_directly(self):
        image_url = (
            "https://cdn.discordapp.com/attachments/"
            "123/456/game-night-wheel.gif"
        )
        embed = create_hosted_spin_embed(
            "multiplayer",
            image_url,
        )

        self.assertEqual(
            embed.image.url,
            image_url,
        )
        self.assertFalse(
            embed.image.url.startswith(
                "attachment://"
            )
        )

    def test_reroll_skips_a_prepared_copy_of_current_game(self):
        manager = PreparedSpinManager(
            SimpleNamespace(http_session=None)
        )
        first = _prepared_spin(10)
        second = _prepared_spin(20)
        manager._ready["multiplayer"].extend(
            (first, second)
        )

        selected, expired = manager._take_ready_locked(
            "multiplayer",
            exclude_game_id=10,
        )

        self.assertEqual(selected, second)
        self.assertEqual(expired, [])
        self.assertEqual(
            list(manager._ready["multiplayer"]),
            [first],
        )

    def test_expired_prepared_spins_are_not_served(self):
        manager = PreparedSpinManager(
            SimpleNamespace(http_session=None)
        )
        expired_item = _prepared_spin(
            10,
            created_at=(
                time.monotonic()
                - PREPARED_SPIN_MAX_AGE_SECONDS
                - 1
            ),
        )
        manager._ready["multiplayer"].append(
            expired_item
        )

        selected, expired = manager._take_ready_locked(
            "multiplayer",
            exclude_game_id=None,
        )

        self.assertIsNone(selected)
        self.assertEqual(expired, [expired_item])
        self.assertEqual(
            list(manager._ready["multiplayer"]),
            [],
        )


class SessionPreparedSpinTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_session_acquire_uses_only_its_generation(self):
        manager = PreparedSpinManager(
            SimpleNamespace(http_session=None)
        )
        manager.enabled = True
        manager._client = object()
        item = _prepared_spin(20)
        item = PreparedSpin(
            **{
                **item.__dict__,
                "session_id": 12,
                "generation": 3,
                "player_count": 6,
            }
        )
        manager._session_pools[12] = SessionSpinPool(
            session_id=12,
            generation=3,
            player_count=6,
            include_unverified=False,
            use_normal_wheel=False,
            eligible_game_ids=frozenset({20}),
            eligibility={
                "eligible_count": 1,
            },
            ready=deque([item]),
        )
        manager._schedule_mark_consumed = Mock()
        manager._schedule_delete = Mock()
        manager._schedule_session_card_refresh = Mock()

        stale = await manager.acquire_session(
            12,
            generation=2,
        )
        selected = await manager.acquire_session(
            12,
            generation=3,
        )

        self.assertIsNone(stale)
        self.assertEqual(selected, item)
        self.assertEqual(
            len(manager._session_pools[12].ready),
            0,
        )

    async def test_removing_session_pool_deletes_unused_items(self):
        manager = PreparedSpinManager(
            SimpleNamespace(http_session=None)
        )
        first = _prepared_spin(10)
        second = _prepared_spin(20)
        manager._session_pools[55] = SessionSpinPool(
            session_id=55,
            generation=1,
            player_count=4,
            include_unverified=False,
            use_normal_wheel=False,
            eligible_game_ids=frozenset({10, 20}),
            eligibility={
                "eligible_count": 2,
            },
            ready=deque(
                [first, second]
            ),
        )
        manager._schedule_delete = Mock()

        removed = await manager.remove_session_pool(55)

        self.assertEqual(removed, 2)
        self.assertNotIn(55, manager._session_pools)
        self.assertEqual(
            manager._schedule_delete.call_count,
            2,
        )

class PreparedSpinLibraryTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_library_invalidation_clears_both_pools(self):
        manager = PreparedSpinManager(
            SimpleNamespace(http_session=None)
        )
        multiplayer = _prepared_spin(10)
        singleplayer = PreparedSpin(
            winning_game=(
                20,
                "Game 20",
                "https://store.example/game",
                "Steam",
            ),
            wheel_type="singleplayer",
            object_name="prepared/singleplayer/20.gif",
            image_url="https://objects.example/20.gif",
            duration_seconds=8.0,
            created_at=time.monotonic(),
        )
        manager._ready["multiplayer"].append(multiplayer)
        manager._ready["singleplayer"].append(singleplayer)
        manager._wheel_game_ids = {
            "multiplayer": frozenset({10}),
            "singleplayer": frozenset({20}),
        }
        manager._schedule_delete = Mock()

        removed = await manager.invalidate_library()

        self.assertEqual(removed, 2)
        self.assertEqual(
            list(manager._ready["multiplayer"]),
            [],
        )
        self.assertEqual(
            list(manager._ready["singleplayer"]),
            [],
        )
        self.assertIsNone(manager._wheel_game_ids)
        self.assertEqual(manager._library_generation, 1)
        self.assertEqual(
            manager._schedule_delete.call_count,
            2,
        )
        self.assertTrue(manager._wake_event.is_set())

    @patch(
        "utils.prepared_spins.get_spin_games_by_ids",
        new_callable=AsyncMock,
    )
    @patch(
        "utils.prepared_spins.get_wheel_game_ids",
        new_callable=AsyncMock,
    )
    async def test_restart_restores_valid_discord_cache(
        self,
        get_wheel_ids,
        get_games,
    ):
        bot_user = object()
        game = (
            42,
            "Cached Game",
            "https://store.example/game",
            "Steam",
            "Tester",
            0,
            None,
            "https://images.example/game.jpg",
            4,
            "https://images.example/game.jpg",
            None,
            None,
            None,
            None,
            None,
        )
        message = SimpleNamespace(
            id=123,
            author=bot_user,
            content=(
                "Prepared Game Night spin | multiplayer | "
                "winner 42"
            ),
            created_at=datetime.now(timezone.utc),
            attachments=[
                SimpleNamespace(
                    filename=(
                        "game-night-wheel-multiplayer-"
                        "0123456789abcdefabcd.gif"
                    ),
                    url=(
                        "https://cdn.discordapp.com/"
                        "attachments/1/2/spin.gif"
                    ),
                )
            ],
            delete=AsyncMock(),
        )
        channel = SimpleNamespace(
            history=lambda **_kwargs: _AsyncMessageHistory(
                [message]
            )
        )
        manager = PreparedSpinManager(
            SimpleNamespace(
                user=bot_user,
                http_session=None,
            )
        )
        manager.namespace = "example-namespace"
        manager.bucket = "example-bucket"
        manager.region = "us-phoenix-1"
        manager._cache_channel = channel
        get_wheel_ids.return_value = {
            "multiplayer": frozenset({42}),
            "singleplayer": frozenset(),
        }
        get_games.return_value = {42: game}

        restored, stale = (
            await manager._restore_cache_messages()
        )

        self.assertEqual(restored, 1)
        self.assertEqual(stale, 0)
        self.assertEqual(
            len(manager._ready["multiplayer"]),
            1,
        )
        item = manager._ready["multiplayer"][0]
        self.assertEqual(item.winner_id, 42)
        self.assertEqual(
            item.duration_seconds,
            PREPARED_SPIN_DURATION_SECONDS,
        )
        self.assertEqual(item.cache_message_id, 123)
        self.assertIn(
            item.object_name,
            manager._known_object_names,
        )
        message.delete.assert_not_awaited()

    @patch(
        "utils.prepared_spins.get_spin_games_by_ids",
        new_callable=AsyncMock,
    )
    @patch(
        "utils.prepared_spins.get_wheel_game_ids",
        new_callable=AsyncMock,
    )
    async def test_restart_deletes_consumed_cache_entry(
        self,
        get_wheel_ids,
        get_games,
    ):
        bot_user = object()
        message = SimpleNamespace(
            id=456,
            author=bot_user,
            content=(
                "Consumed Game Night spin | multiplayer | "
                "winner 42"
            ),
            created_at=datetime.now(timezone.utc),
            attachments=[
                SimpleNamespace(
                    filename=(
                        "game-night-wheel-multiplayer-"
                        "0123456789abcdefabcd.gif"
                    ),
                    url="https://cdn.example/spin.gif",
                )
            ],
            delete=AsyncMock(),
        )
        manager = PreparedSpinManager(
            SimpleNamespace(
                user=bot_user,
                http_session=None,
            )
        )
        manager._cache_channel = SimpleNamespace(
            history=lambda **_kwargs: _AsyncMessageHistory(
                [message]
            )
        )
        manager._delete_object = AsyncMock()
        get_wheel_ids.return_value = {
            "multiplayer": frozenset({42}),
            "singleplayer": frozenset(),
        }
        get_games.return_value = {}

        restored, stale = (
            await manager._restore_cache_messages()
        )

        self.assertEqual(restored, 0)
        self.assertEqual(stale, 1)
        message.delete.assert_awaited_once_with()
        manager._delete_object.assert_awaited_once_with(
            "prepared/multiplayer/"
            "game-night-wheel-0123456789abcdefabcd.gif"
        )


class PreparedSpinInvalidationTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_locked_game_is_removed_from_every_ready_pool(self):
        manager = PreparedSpinManager(
            SimpleNamespace(http_session=None)
        )
        multiplayer_item = _prepared_spin(10)
        singleplayer_item = PreparedSpin(
            winning_game=multiplayer_item.winning_game,
            wheel_type="singleplayer",
            object_name="prepared/singleplayer/10.gif",
            image_url=multiplayer_item.image_url,
            duration_seconds=8.0,
            created_at=time.monotonic(),
        )
        manager._ready["multiplayer"].append(
            multiplayer_item
        )
        manager._ready["singleplayer"].append(
            singleplayer_item
        )
        scheduled = []
        manager._schedule_delete = (
            lambda object_name, *, cache_message_id=None,
            delay_seconds: (
                scheduled.append(
                    (
                        object_name,
                        cache_message_id,
                        delay_seconds,
                    )
                )
            )
        )

        removed = await manager.invalidate_game(10)

        self.assertEqual(removed, 2)
        self.assertEqual(
            list(manager._ready["multiplayer"]),
            [],
        )
        self.assertEqual(
            list(manager._ready["singleplayer"]),
            [],
        )
        self.assertEqual(
            {name for name, _message_id, _delay in scheduled},
            {
                multiplayer_item.object_name,
                singleplayer_item.object_name,
            },
        )

    async def test_changed_wheel_membership_rebuilds_its_pool(self):
        manager = PreparedSpinManager(
            SimpleNamespace(http_session=None)
        )
        multiplayer_item = _prepared_spin(10)
        singleplayer_item = PreparedSpin(
            winning_game=(20, "Game 20"),
            wheel_type="singleplayer",
            object_name="prepared/singleplayer/20.gif",
            image_url="https://objects.example/20.gif",
            duration_seconds=8.0,
            created_at=time.monotonic(),
        )
        manager._ready["multiplayer"].append(
            multiplayer_item
        )
        manager._ready["singleplayer"].append(
            singleplayer_item
        )
        manager._wheel_game_ids = {
            "multiplayer": frozenset({10}),
            "singleplayer": frozenset({20}),
        }
        scheduled = []
        manager._schedule_delete = (
            lambda object_name, *, cache_message_id=None,
            delay_seconds: (
                scheduled.append(
                    (
                        object_name,
                        cache_message_id,
                        delay_seconds,
                    )
                )
            )
        )

        with patch(
            "utils.prepared_spins.get_wheel_game_ids",
            new=AsyncMock(
                return_value={
                    "multiplayer": frozenset({10, 30}),
                    "singleplayer": frozenset({20}),
                }
            ),
        ):
            rebuilt = await manager._refresh_pool_state()

        self.assertTrue(rebuilt)
        self.assertEqual(
            list(manager._ready["multiplayer"]),
            [],
        )
        self.assertEqual(
            list(manager._ready["singleplayer"]),
            [singleplayer_item],
        )
        self.assertEqual(
            scheduled,
            [(multiplayer_item.object_name, None, 0)],
        )

    async def test_full_buffer_needs_one_replacement_after_use(self):
        manager = PreparedSpinManager(
            SimpleNamespace(http_session=None)
        )
        manager._ready["multiplayer"].extend(
            _prepared_spin(game_id)
            for game_id in range(
                1,
                PREPARED_SPIN_POOL_SIZE + 1,
            )
        )

        self.assertFalse(
            await manager._needs_item("multiplayer")
        )
        manager._ready["multiplayer"].popleft()
        self.assertTrue(
            await manager._needs_item("multiplayer")
        )
        self.assertEqual(
            PREPARED_SPIN_POOL_SIZE,
            20,
        )

    async def test_cache_upload_returns_discord_attachment_url(self):
        manager = PreparedSpinManager(
            SimpleNamespace(http_session=None)
        )
        cache_url = (
            "https://cdn.discordapp.com/attachments/"
            "123/456/game-night-wheel.gif"
        )
        cache_message = SimpleNamespace(
            id=456,
            attachments=[
                SimpleNamespace(url=cache_url)
            ],
        )
        manager._cache_channel = SimpleNamespace(
            send=AsyncMock(
                return_value=cache_message
            )
        )

        with patch(
            "utils.prepared_spins.discord.File",
            return_value=object(),
        ):
            message_id, image_url = (
                await manager._upload_cache_file(
                    "prepared.gif",
                    wheel_type="multiplayer",
                    winner_id=10,
                    token="test",
                )
            )

        self.assertEqual(message_id, 456)
        self.assertEqual(image_url, cache_url)
        self.assertIn(
            456,
            manager._known_cache_message_ids,
        )

    async def test_delayed_cleanup_removes_oracle_and_discord_copy(self):
        manager = PreparedSpinManager(
            SimpleNamespace(http_session=None)
        )
        manager._delete_object = AsyncMock()
        manager._delete_cache_message = AsyncMock()

        await manager._delete_later(
            "prepared/multiplayer/test.gif",
            cache_message_id=456,
            delay_seconds=0,
        )

        manager._delete_object.assert_awaited_once_with(
            "prepared/multiplayer/test.gif"
        )
        manager._delete_cache_message.assert_awaited_once_with(
            456
        )

    async def test_missing_oracle_object_is_already_clean(self):
        class MissingObjectError(Exception):
            status = 404
            code = "ObjectNotFound"

        object_name = "prepared/multiplayer/missing.gif"
        manager = PreparedSpinManager(
            SimpleNamespace(http_session=None)
        )
        manager._client = SimpleNamespace(
            delete_object=Mock(
                side_effect=MissingObjectError()
            )
        )
        manager._known_object_names.add(object_name)

        await manager._delete_object(object_name)

        self.assertNotIn(
            object_name,
            manager._known_object_names,
        )


if __name__ == "__main__":
    unittest.main()
