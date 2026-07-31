import io
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageChops

from utils.spin_gif import (
    CANVAS_HEIGHT,
    CANVAS_WIDTH,
    MAX_SPIN_GIF_BYTES,
    SPIN_FRAME_DURATIONS_MS,
    SUSPENSE_DURATION_MS,
    WINNER_FLASH_DURATION_MS,
    _artwork_panel,
    _winner_frame,
    GOLD_COLOUR,
    build_spin_sequence,
    build_spin_gif,
)


class SpinGifTests(unittest.TestCase):
    def test_artwork_panel_preserves_the_complete_image(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            artwork_path = (
                Path(temporary_directory)
                / "complete-artwork.png"
            )
            artwork = Image.new(
                "RGB",
                (200, 200),
                (40, 180, 70),
            )

            for x_position in range(200):
                for y_position in range(20):
                    artwork.putpixel(
                        (x_position, y_position),
                        (230, 40, 40),
                    )
                    artwork.putpixel(
                        (x_position, 199 - y_position),
                        (40, 80, 230),
                    )

            artwork.save(
                artwork_path,
                format="PNG",
            )
            panel = _artwork_panel(
                artwork_path
            )
            centre_x = panel.width // 2

            self.assertEqual(
                panel.getpixel((centre_x, 2))[:3],
                (230, 40, 40),
            )
            self.assertEqual(
                panel.getpixel(
                    (centre_x, panel.height - 3)
                )[:3],
                (40, 80, 230),
            )

    def test_consecutive_spins_get_different_sequences(self):
        games = [
            {
                "id": game_id,
                "name": f"Game {game_id}",
                "store": "Steam",
            }
            for game_id in range(1, 21)
        ]
        winner = {
            "id": 99,
            "name": "Winner",
            "store": "Steam",
        }

        first_sequence = build_spin_sequence(
            games,
            winner,
            wheel_type="sequence-test",
        )
        second_sequence = build_spin_sequence(
            games,
            winner,
            wheel_type="sequence-test",
        )

        first_ids = [
            game["id"]
            for game in first_sequence
        ]
        second_ids = [
            game["id"]
            for game in second_sequence
        ]

        self.assertEqual(
            len(first_ids),
            len(SPIN_FRAME_DURATIONS_MS),
        )
        self.assertEqual(
            len(second_ids),
            len(SPIN_FRAME_DURATIONS_MS),
        )
        self.assertNotEqual(first_ids, second_ids)
        self.assertNotIn(winner["id"], first_ids)
        self.assertNotIn(winner["id"], second_ids)

    def test_builds_one_animated_file_with_all_text_frames(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            artwork_paths = []

            for index, colour in enumerate(
                (
                    (180, 60, 60),
                    (60, 150, 210),
                    (90, 180, 100),
                )
            ):
                artwork_path = root / f"art-{index}.jpg"
                Image.new(
                    "RGB",
                    (460, 215),
                    colour,
                ).save(
                    artwork_path,
                    format="JPEG",
                )
                artwork_paths.append(artwork_path)

            sequence = [
                {
                    "id": index + 1,
                    "name": (
                        "A Very Long Test Game Name That "
                        f"Still Needs To Fit Frame {index + 1}"
                    ),
                    "store": (
                        "Steam"
                        if index % 2 == 0
                        else "Epic Games Store"
                    ),
                    "artwork_path": str(
                        artwork_paths[
                            index % len(artwork_paths)
                        ]
                    ),
                }
                for index in range(
                    len(SPIN_FRAME_DURATIONS_MS)
                )
            ]
            winner = {
                "id": 99,
                "name": "The Winning Game",
                "store": "Steam",
                "artwork_path": str(
                    artwork_paths[0]
                ),
            }

            result = build_spin_gif(
                sequence,
                winner,
            )

            self.assertTrue(
                result.data.startswith(b"GIF")
            )
            self.assertLess(
                len(result.data),
                MAX_SPIN_GIF_BYTES,
            )

            expected_duration = (
                sum(SPIN_FRAME_DURATIONS_MS)
                + SUSPENSE_DURATION_MS
                + WINNER_FLASH_DURATION_MS
            ) / 1000
            self.assertAlmostEqual(
                result.duration_seconds,
                expected_duration,
            )

            with Image.open(
                io.BytesIO(result.data)
            ) as animation:
                self.assertEqual(
                    animation.size,
                    (CANVAS_WIDTH, CANVAS_HEIGHT),
                )
                self.assertTrue(
                    animation.is_animated
                )
                self.assertEqual(
                    animation.n_frames,
                    len(SPIN_FRAME_DURATIONS_MS) + 2,
                )
                animation.seek(
                    animation.n_frames - 1
                )
                decoded_winner = animation.convert(
                    "RGB"
                )
                expected_winner = _winner_frame(
                    winner,
                    accent_colour=GOLD_COLOUR,
                ).quantize(
                    colors=96,
                    method=Image.Quantize.FASTOCTREE,
                    dither=Image.Dither.FLOYDSTEINBERG,
                ).convert("RGB")
                self.assertIsNone(
                    ImageChops.difference(
                        decoded_winner,
                        expected_winner,
                    ).getbbox()
                )

    def test_missing_artwork_uses_a_placeholder(self):
        sequence = [
            {
                "id": index + 1,
                "name": f"No Artwork Game {index + 1}",
                "store": "Unknown Store",
                "artwork_path": None,
            }
            for index in range(
                len(SPIN_FRAME_DURATIONS_MS)
            )
        ]
        winner = {
            "id": 99,
            "name": "No Artwork Winner",
            "store": "Unknown Store",
            "artwork_path": None,
        }

        result = build_spin_gif(
            sequence,
            winner,
            wheel_type="singleplayer",
        )

        self.assertTrue(result.data.startswith(b"GIF"))
        self.assertLess(
            len(result.data),
            MAX_SPIN_GIF_BYTES,
        )


if __name__ == "__main__":
    unittest.main()
