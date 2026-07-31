import csv
import io
import unittest

from utils.metadata_audit import (
    audit_game_metadata,
    build_metadata_audit_report,
)


def _complete_game(**changes):
    game = {
        "name": "Bodycam",
        "store": "Steam",
        "store_link": (
            "https://store.steampowered.com/app/2406770/"
        ),
        "external_id": "2406770",
        "image_url": "https://example.com/bodycam.jpg",
        "link_status": "live",
        "availability_status": "released",
        "release_date": None,
        "max_players": 10,
        "max_players_source": "IGDB",
        "igdb_id": 12345,
        "multiplayer_support_json": (
            '{"online_multiplayer":true,'
            '"online_max":10,"team_format":"5v5"}'
        ),
        "genres_json": '["Shooter","Action"]',
        "game_modes_json": '["Multiplayer"]',
        "library_section": "Multiplayer wheel",
    }
    game.update(changes)
    return game


class MetadataAuditTests(unittest.TestCase):
    def test_complete_multiplayer_game_has_no_gaps(self):
        audit = audit_game_metadata(
            _complete_game()
        )

        self.assertEqual(audit["missing"], [])

    def test_supported_online_mode_without_limit_is_flagged(self):
        audit = audit_game_metadata(
            _complete_game(
                max_players=None,
                max_players_source=None,
                multiplayer_support_json=(
                    '{"online_multiplayer":true}'
                ),
            )
        )

        self.assertIn(
            "overall player limit",
            audit["missing"],
        )
        self.assertIn(
            "online multiplayer limit",
            audit["missing"],
        )

    def test_known_single_player_game_does_not_require_support(self):
        audit = audit_game_metadata(
            _complete_game(
                name="Solo Game",
                max_players=1,
                multiplayer_support_json="{}",
                game_modes_json='["Single player"]',
                library_section="Single-player wheel",
            )
        )

        self.assertNotIn(
            "multiplayer support",
            audit["missing"],
        )

    def test_variable_capacity_is_complete_without_fixed_limit(self):
        audit = audit_game_metadata(
            _complete_game(
                name="s&box",
                max_players=None,
                max_players_source=None,
                igdb_id=None,
                multiplayer_support_json=(
                    '{"online_multiplayer":true,'
                    '"variable_capacity":true}'
                ),
            )
        )

        self.assertNotIn(
            "overall player limit",
            audit["missing"],
        )
        self.assertNotIn(
            "online multiplayer limit",
            audit["missing"],
        )
        self.assertNotIn("IGDB match", audit["missing"])

    def test_unannounced_wishlist_capacity_is_not_a_gap(self):
        audit = audit_game_metadata(
            _complete_game(
                name="Upcoming Co-op",
                availability_status="coming_soon",
                release_date="Coming soon",
                max_players=None,
                max_players_source=None,
                multiplayer_support_json=(
                    '{"online_coop":true,'
                    '"capacity_tba":true}'
                ),
            )
        )

        self.assertNotIn(
            "overall player limit",
            audit["missing"],
        )
        self.assertNotIn(
            "online co-op limit",
            audit["missing"],
        )

    def test_report_contains_every_game_and_gap_counts(self):
        complete = _complete_game()
        incomplete = _complete_game(
            name="Unknown Players",
            max_players=None,
            max_players_source=None,
            multiplayer_support_json="{}",
        )

        report, summary = build_metadata_audit_report(
            [complete, incomplete]
        )
        rows = list(
            csv.DictReader(
                io.StringIO(report)
            )
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual(
            rows[0]["Missing metadata"],
            "Complete",
        )
        self.assertIn(
            "overall player limit",
            rows[1]["Missing metadata"],
        )
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["complete"], 1)
        self.assertEqual(summary["incomplete"], 1)


if __name__ == "__main__":
    unittest.main()
