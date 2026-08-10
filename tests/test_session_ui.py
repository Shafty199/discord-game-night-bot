import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from commands.sessions import (
    SessionCacheView,
    Sessions,
    SessionView,
    _wheel_library_changed,
)


def session_record(**overrides) -> dict:
    session = {
        "id": 12,
        "guild_id": 100,
        "host_id": 200,
        "voice_channel_id": 300,
        "members": [
            {
                "user_id": 200,
                "display_name": "Host",
            }
        ],
        "effective_player_count": 1,
        "manual_player_count": None,
        "include_unverified": True,
        "use_normal_wheel": False,
        "selected_game_id": None,
        "custom_game_name": None,
        "custom_game_link": None,
    }
    session.update(overrides)
    return session


class SessionCardVisibilityTests(
    unittest.IsolatedAsyncioTestCase
):
    def test_cache_command_defaults_to_manage_server_staff(self):
        command = Sessions.__dict__["cache"]

        self.assertTrue(
            command.default_permissions.manage_guild
        )

    def test_duplicate_add_does_not_rebuild_spin_cache(self):
        self.assertFalse(
            _wheel_library_changed({"status": "unchanged"})
        )
        self.assertTrue(
            _wheel_library_changed({"status": "added"})
        )

    def test_public_view_never_shows_rebuild_cache(self):
        view = SessionView(
            SimpleNamespace(),
            session_record(),
        )
        labels = {
            child.label
            for child in view.children
        }

        self.assertNotIn("Rebuild Cache", labels)
        self.assertNotIn("Return to Wheel", labels)
        self.assertNotIn("Finished Playing", labels)
        self.assertIn("Transfer Host", labels)

    def test_public_view_keeps_return_to_wheel_after_selection(self):
        view = SessionView(
            SimpleNamespace(),
            session_record(custom_game_name="Chosen Game"),
        )
        labels = {
            child.label
            for child in view.children
        }

        self.assertNotIn("Rebuild Cache", labels)
        self.assertIn("Return to Wheel", labels)
        self.assertIn("Finished Playing", labels)

    def test_private_staff_view_contains_cache_control(self):
        view = SessionCacheView(
            SimpleNamespace(),
            session_record(),
        )
        labels = {
            child.label
            for child in view.children
        }

        self.assertEqual(labels, {"Rebuild Cache"})

    async def test_public_embed_omits_cache_details(self):
        manager = SimpleNamespace(
            enabled=True,
            get_session_pool_status=AsyncMock(),
        )
        cog = Sessions(
            SimpleNamespace(
                prepared_spin_manager=manager
            )
        )

        with patch(
            "commands.sessions.get_session_wheel_game_ids",
            new=AsyncMock(
                return_value={"eligible_count": 46}
            ),
        ):
            embed = await cog._build_embed(
                session_record()
            )
        field_names = {
            field.name
            for field in embed.fields
        }
        fields = {
            field.name: field.value
            for field in embed.fields
        }

        self.assertNotIn("Temporary Cache", field_names)
        self.assertEqual(
            fields["Games on Wheel"],
            "**46 eligible games**",
        )
        self.assertNotIn("Session #", embed.footer.text)
        manager.get_session_pool_status.assert_not_awaited()

    async def test_ending_deletes_voice_room_backlink(self):
        message = SimpleNamespace(delete=AsyncMock())
        channel = SimpleNamespace(
            send=AsyncMock(),
            fetch_message=AsyncMock(
                return_value=message
            ),
        )
        guild = SimpleNamespace(
            get_channel=lambda _channel_id: channel
        )
        cog = Sessions(
            SimpleNamespace(
                get_guild=lambda _guild_id: guild
            )
        )

        await cog._delete_voice_session_notice(
            session_record(
                voice_notice_message_id=987,
            )
        )

        channel.fetch_message.assert_awaited_once_with(987)
        message.delete.assert_awaited_once()

    async def test_non_staff_cannot_open_cache_controls(self):
        response = SimpleNamespace(
            send_message=AsyncMock()
        )
        interaction = SimpleNamespace(
            user=SimpleNamespace(
                guild_permissions=SimpleNamespace(
                    manage_guild=False,
                    administrator=False,
                )
            ),
            response=response,
        )
        cog = Sessions(SimpleNamespace())

        allowed = await cog.can_manage_session_cache(
            interaction
        )

        self.assertFalse(allowed)
        response.send_message.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
