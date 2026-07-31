import json
import random
import re
from urllib.parse import urlparse

import discord

from settings import EPIC_EMOJI, STEAM_EMOJI
from utils.time_utils import parse_stored_datetime


FRESH_PICK_LINES = [
    "✨ A fresh pick — this game hasn't been played yet!",
    "👀 This one's been patiently waiting for its turn.",
    "🆕 Brand-new territory for Game Night.",
    "🎲 The wheel has uncovered something new.",
    "🔥 Fresh from the backlog and ready to go.",
]


RETURNING_GAME_LINES = [
    "⚔️ Time for a rematch!",
    "🔁 The wheel has called for another round.",
    "🎮 An old contender returns.",
    "🏆 Back for another shot at Game Night glory.",
    "👀 Apparently the wheel wasn't finished with this one.",
]


GENERAL_FLAVOUR_LINES = [
    "🎲 Fate has officially made the decision.",
    "🎉 Get the group ready.",
    "🕹️ Controllers charged? Good.",
    "🍕 Sort the snacks — the game has been chosen.",
    "🎧 Headsets on. Game Night awaits.",
    "👀 Complaints may be submitted directly to the wheel.",
    "🏁 The debate is over. Probably.",
]


def _clean_text(
    value,
    fallback: str = "",
) -> str:
    if value is None:
        return fallback

    cleaned_value = str(
        value
    ).strip()

    return cleaned_value or fallback


def _clean_url(
    value,
) -> str | None:
    """
    Return a Discord-safe absolute HTTP or HTTPS URL.

    Invalid values are ignored rather than allowing
    Discord to reject the entire embed.
    """

    if value is None:
        return None

    cleaned_value = str(
        value
    ).strip()

    if not cleaned_value:
        return None

    cleaned_value = cleaned_value.strip(
        "<>[]()\"'"
    ).strip()

    if re.search(
        r"[\s\x00-\x1f\x7f]",
        cleaned_value,
    ):
        return None

    if not cleaned_value.lower().startswith(
        (
            "https://",
            "http://",
        )
    ):
        return None

    try:
        parsed_url = urlparse(
            cleaned_value
        )

    except ValueError:
        return None

    if parsed_url.scheme.lower() not in {
        "http",
        "https",
    }:
        return None

    if not parsed_url.netloc:
        return None

    hostname = parsed_url.hostname

    if not hostname:
        return None

    if (
        "." not in hostname
        and hostname != "localhost"
    ):
        return None

    try:
        parsed_url.port

    except ValueError:
        return None

    return cleaned_value


def _safe_integer(
    value,
    fallback: int = 0,
) -> int:
    try:
        return int(
            value
        )

    except (
        TypeError,
        ValueError,
    ):
        return fallback


def _game_json_metadata(
    game,
    index: int,
    expected_type,
):
    if len(game) <= index:
        return expected_type()

    value = game[index]

    if isinstance(value, expected_type):
        return value

    if not isinstance(value, str) or not value:
        return expected_type()

    try:
        value = json.loads(value)

    except (TypeError, ValueError):
        return expected_type()

    return (
        value
        if isinstance(value, expected_type)
        else expected_type()
    )


def _game_max_players(
    game,
) -> int | None:
    if len(game) <= 8:
        return None

    max_players = _safe_integer(
        game[8],
        0,
    )
    return (
        max_players
        if 1 <= max_players <= 100
        else None
    )


def _format_multiplayer_support(
    support: dict,
    max_players: int | None = None,
) -> str | None:
    lines = []
    detailed_counts = set()
    online_coop_count = _safe_integer(
        support.get("online_coop_max"),
        0,
    )
    online_count = _safe_integer(
        support.get("online_max"),
        0,
    )
    offline_coop_count = _safe_integer(
        support.get("offline_coop_max"),
        0,
    )
    offline_count = _safe_integer(
        support.get("offline_max"),
        0,
    )
    count_modes = (
        (
            "online_coop_max",
            "online_coop",
            "🌐 Online co-op",
        ),
        (
            "online_max",
            "online_multiplayer",
            "🌐 Online multiplayer",
        ),
        (
            "offline_coop_max",
            "offline_coop",
            "🏠 Local co-op",
        ),
        (
            "offline_max",
            "offline_multiplayer",
            "🏠 Local multiplayer",
        ),
    )

    for count_key, support_key, label in count_modes:
        count = _safe_integer(
            support.get(count_key),
            0,
        )

        if (
            count_key == "online_max"
            and online_coop_count >= 2
            and online_count == online_coop_count
        ):
            continue

        if (
            count_key == "offline_max"
            and offline_coop_count >= 2
            and offline_count == offline_coop_count
        ):
            continue

        if count >= 2:
            detailed_counts.add(count)
            lines.append(
                f"{label}: **up to {count} players**"
            )

        elif support_key and support.get(support_key):
            lines.append(f"{label}: **supported**")

    team_format = str(
        support.get("team_format") or ""
    ).strip()

    if not team_format:
        team_sizes = support.get("team_sizes")

        if isinstance(team_sizes, list):
            clean_team_sizes = [
                _safe_integer(value, 0)
                for value in team_sizes
            ]

            if (
                len(clean_team_sizes) >= 2
                and all(
                    1 <= value <= 50
                    for value in clean_team_sizes
                )
            ):
                team_format = "v".join(
                    str(value)
                    for value in clean_team_sizes
                )

    if not team_format:
        team_count = _safe_integer(
            support.get("team_count"),
            0,
        )
        team_size = _safe_integer(
            support.get("team_size"),
            0,
        )

        if team_count >= 2 and team_size >= 1:
            team_format = (
                f"{team_count} teams of {team_size}"
            )

        elif team_size >= 1:
            team_format = (
                f"up to {team_size} per team"
            )

    team_format = re.sub(
        r"\s+",
        " ",
        team_format,
    ).strip()

    if team_format and len(team_format) <= 40:
        lines.append(
            f"⚔️ Teams: **{team_format}**"
        )

    extra_features = [
        label
        for key, label in (
            ("campaign_coop", "Campaign co-op"),
            ("drop_in", "Drop-in/out"),
            ("lan_coop", "LAN co-op"),
            ("mmo", "MMO"),
            ("split_screen", "Split-screen"),
            (
                "split_screen_online",
                "Online split-screen",
            ),
        )
        if support.get(key)
    ]

    if extra_features:
        lines.append(
            "✨ " + " • ".join(extra_features)
        )

    if (
        max_players is None
        and (
            support.get("mmo")
            or support.get("variable_capacity")
        )
    ):
        lines.insert(
            0,
            (
                "👥 Player limit: "
                "**varies by game/server**"
            ),
        )

    elif (
        max_players is None
        and support.get("capacity_tba")
    ):
        lines.insert(
            0,
            (
                "👥 Player limit: "
                "**not announced yet**"
            ),
        )

    if (
        max_players is not None
        and max_players >= 2
        and max_players not in detailed_counts
    ):
        lines.insert(
            0,
            (
                "👥 Player limit: "
                f"**up to {max_players} players**"
            ),
        )

    elif max_players == 1 and not lines:
        lines.append(
            "🧍 **Single-player only**"
        )

    return "\n".join(lines) or None


def format_game_multiplayer_support(
    game,
) -> str | None:
    return _format_multiplayer_support(
        _game_json_metadata(
            game,
            11,
            dict,
        ),
        _game_max_players(game),
    )


def _deduplicate_game_modes(
    game_modes: list,
    multiplayer_support: dict,
) -> list:
    normalised_modes = {
        str(mode).strip().casefold()
        for mode in game_modes
    }

    if not {
        "multiplayer",
        "co-operative",
    }.issubset(normalised_modes):
        return game_modes

    matching_capacity_checks = []

    for coop_key, multiplayer_key in (
        ("online_coop_max", "online_max"),
        ("offline_coop_max", "offline_max"),
    ):
        coop_count = _safe_integer(
            multiplayer_support.get(coop_key),
            0,
        )
        multiplayer_count = _safe_integer(
            multiplayer_support.get(
                multiplayer_key
            ),
            0,
        )

        if coop_count >= 2 and multiplayer_count >= 2:
            matching_capacity_checks.append(
                coop_count == multiplayer_count
            )

    if (
        matching_capacity_checks
        and all(matching_capacity_checks)
    ):
        return [
            mode
            for mode in game_modes
            if str(mode).strip().casefold()
            != "multiplayer"
        ]

    return game_modes


def _format_last_played(
    last_played,
) -> str:
    if not last_played:
        return "Never"

    try:
        played_date = parse_stored_datetime(
            last_played
        )

        if played_date is None:
            raise ValueError(
                "Invalid stored play timestamp"
            )

        return discord.utils.format_dt(
            played_date,
            style="R",
        )

    except (
        TypeError,
        ValueError,
    ):
        return _clean_text(
            last_played,
            "Unknown",
        )[:20]


def _get_store_details(
    store: str,
) -> tuple[str, discord.Colour]:
    normalised_store = store.casefold()

    if "steam" in normalised_store:
        return (
            f"{STEAM_EMOJI} Steam",
            discord.Colour.blue(),
        )

    if "epic" in normalised_store:
        return (
            f"{EPIC_EMOJI} Epic Games Store",
            discord.Colour.dark_purple(),
        )

    return (
        f"🎮 {store}",
        discord.Colour.gold(),
    )


def _get_flavour_text(
    times_played: int,
) -> str:
    if times_played == 0:
        main_line = random.choice(
            FRESH_PICK_LINES
        )

    else:
        main_line = random.choice(
            RETURNING_GAME_LINES
        )

    extra_line = random.choice(
        GENERAL_FLAVOUR_LINES
    )

    return (
        f"{main_line}\n"
        f"{extra_line}"
    )


def format_sale_text(
    sale_info: dict | None,
) -> str | None:
    if not isinstance(
        sale_info,
        dict,
    ):
        return None

    if not sale_info.get(
        "is_on_sale"
    ):
        return None

    discount_percent = _safe_integer(
        sale_info.get(
            "discount_percent"
        ),
        0,
    )

    final_price = _clean_text(
        sale_info.get(
            "final_price"
        ),
    )

    original_price = _clean_text(
        sale_info.get(
            "original_price"
        ),
    )

    if discount_percent <= 0 or not final_price:
        return None

    price_line = (
        f"**{discount_percent}% off — {final_price}**"
    )

    if (
        original_price
        and original_price != final_price
    ):
        price_line += (
            f"\nUsually ~~{original_price}~~"
        )

    return price_line


def create_spin_embed(
    game,
    sale_info: dict | None = None,
    wheel_type: str = "multiplayer",
) -> discord.Embed:
    """
    Expected game tuple:

    0: id
    1: name
    2: store_link
    3: store
    4: suggested_by
    5: times_played
    6: last_played
    7: image_url
    """

    name = _clean_text(
        game[1],
        "Unknown Game",
    )

    store_link = _clean_url(
        game[2]
    )

    store = _clean_text(
        game[3],
        "Unknown Store",
    )

    suggested_by = _clean_text(
        game[4],
        "Unknown",
    )

    times_played = _safe_integer(
        game[5],
        0,
    )

    last_played = game[6]

    image_url = _clean_url(
        game[7]
    )

    genres = _game_json_metadata(
        game,
        12,
        list,
    )
    game_modes = _game_json_metadata(
        game,
        14,
        list,
    )
    multiplayer_support = _game_json_metadata(
        game,
        11,
        dict,
    )
    game_modes = _deduplicate_game_modes(
        game_modes,
        multiplayer_support,
    )

    store_display, embed_colour = (
        _get_store_details(
            store
        )
    )

    flavour_text = _get_flavour_text(
        times_played
    )

    if times_played == 0:
        history_text = (
            "🆕 **Game Night debut**\n"
            "This one has never been locked in before."
        )

    else:
        play_word = (
            "time"
            if times_played == 1
            else "times"
        )

        history_text = (
            f"🎲 Played **{times_played} {play_word}**\n"
            f"🕒 Last played "
            f"{_format_last_played(last_played)}"
        )

    singleplayer = (
        wheel_type == "singleplayer"
    )

    embed = discord.Embed(
        title=(
            "🧍 THE SINGLE PLAYER WHEEL HAS SPOKEN!"
            if singleplayer
            else "🎡 THE WHEEL HAS SPOKEN!"
        ),
        description=(
            f"# 🎮 {name}\n\n"
            f"{flavour_text}"
        ),
        colour=embed_colour,
    )

    if store_link:
        embed.url = store_link

    if image_url:
        embed.set_image(
            url=image_url
        )

    embed.add_field(
        name="🏪 Platform",
        value=store_display,
        inline=True,
    )

    embed.add_field(
        name="📊 Game Night History",
        value=history_text,
        inline=True,
    )

    multiplayer_text = (
        format_game_multiplayer_support(
            game
        )
    )

    if multiplayer_text:
        embed.add_field(
            name="🤝 Multiplayer Support",
            value=multiplayer_text,
            inline=False,
        )

    if genres:
        embed.add_field(
            name="🏷️ Genres",
            value=" • ".join(
                str(value)
                for value in genres[:3]
            ),
            inline=False,
        )

    if game_modes:
        embed.add_field(
            name="🎮 Game Modes",
            value=" • ".join(
                str(value)
                for value in game_modes[:6]
            ),
            inline=False,
        )

    sale_text = format_sale_text(
        sale_info
    )

    if sale_text:
        embed.add_field(
            name="🏷️ Currently on sale",
            value=sale_text,
            inline=False,
        )

    embed.add_field(
        name="💡 Suggested by",
        value=f"**{suggested_by}**",
        inline=False,
    )

    if store_link:
        embed.add_field(
            name="🚀 Ready to Play?",
            value=(
                f"[Open **{name}** on "
                f"{store}]({store_link})"
            ),
            inline=False,
        )

    else:
        embed.add_field(
            name="🔗 Store Link",
            value=(
                "The saved store link is missing "
                "or could not be validated."
            ),
            inline=False,
        )

    embed.set_footer(
        text=(
            "🔄 Spin Again for another pick  •  "
            "🔒 Lock It In to confirm the winner"
        )
    )

    return embed
