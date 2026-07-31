import csv
import io
import json
from collections import Counter


ONLINE_SUPPORT_FIELDS = {
    "online_coop",
    "online_coop_max",
    "online_multiplayer",
    "online_max",
    "split_screen_online",
}

LOCAL_SUPPORT_FIELDS = {
    "lan_coop",
    "offline_coop",
    "offline_coop_max",
    "offline_multiplayer",
    "offline_max",
    "split_screen",
}

REPORT_COLUMNS = (
    "Game",
    "Store",
    "Library section",
    "Missing metadata",
    "Max players",
    "Player source",
    "Multiplayer support",
    "Genres",
    "Game modes",
    "IGDB ID",
    "External ID",
    "Artwork",
    "Link status",
    "Store link",
)


def _clean_text(value) -> str:
    return str(value or "").strip()


def _positive_int(value) -> int | None:
    try:
        number = int(value)

    except (TypeError, ValueError):
        return None

    return number if number > 0 else None


def _load_saved_json(
    value,
    expected_type,
):
    if isinstance(value, expected_type):
        return value, True

    text = _clean_text(value)

    if not text:
        return expected_type(), True

    try:
        decoded = json.loads(text)

    except (TypeError, ValueError):
        return expected_type(), False

    if not isinstance(decoded, expected_type):
        return expected_type(), False

    return decoded, True


def _has_support_value(
    support: dict,
    field_names: set[str],
) -> bool:
    return any(
        bool(support.get(field_name))
        for field_name in field_names
    )


def _multiplayer_support_summary(
    support: dict,
) -> str:
    lines = []
    count_fields = (
        (
            "online_coop_max",
            "Online co-op",
        ),
        (
            "online_max",
            "Online multiplayer",
        ),
        (
            "offline_coop_max",
            "Local co-op",
        ),
        (
            "offline_max",
            "Local multiplayer",
        ),
    )

    for field_name, label in count_fields:
        count = _positive_int(
            support.get(field_name)
        )

        if count is not None:
            lines.append(
                f"{label}: up to {count}"
            )

    boolean_fields = (
        (
            "online_coop",
            "online_coop_max",
            "Online co-op: supported",
        ),
        (
            "online_multiplayer",
            "online_max",
            "Online multiplayer: supported",
        ),
        (
            "offline_coop",
            "offline_coop_max",
            "Local co-op: supported",
        ),
        (
            "offline_multiplayer",
            "offline_max",
            "Local multiplayer: supported",
        ),
        (
            "lan_coop",
            None,
            "LAN co-op: supported",
        ),
        (
            "split_screen",
            None,
            "Split screen: supported",
        ),
        (
            "split_screen_online",
            None,
            "Online split screen: supported",
        ),
    )

    for flag_name, count_name, label in boolean_fields:
        if not support.get(flag_name):
            continue

        if (
            count_name
            and _positive_int(
                support.get(count_name)
            ) is not None
        ):
            continue

        lines.append(label)

    team_format = _clean_text(
        support.get("team_format")
    )

    if team_format:
        lines.append(
            f"Teams: {team_format}"
        )

    if support.get("mmo"):
        lines.append(
            "MMO / variable capacity"
        )

    elif support.get("variable_capacity"):
        lines.append(
            "Player limit varies by game/server"
        )

    elif support.get("capacity_tba"):
        lines.append(
            "Player limit not announced"
        )

    return " | ".join(lines) or "Not saved"


def _missing_game_metadata(
    game: dict,
    *,
    support: dict,
    support_valid: bool,
    genres: list,
    genres_valid: bool,
    game_modes: list,
    game_modes_valid: bool,
) -> list[str]:
    missing = []

    def add(label: str) -> None:
        if label not in missing:
            missing.append(label)

    if not _clean_text(game.get("store")):
        add("store")

    if not _clean_text(game.get("store_link")):
        add("store link")

    if not _clean_text(game.get("external_id")):
        add("external store ID")

    if not _clean_text(game.get("image_url")):
        add("artwork")

    link_status = _clean_text(
        game.get("link_status")
    ).casefold()

    if link_status in {"", "unknown"}:
        add("store-link verification")
    elif link_status == "dead":
        add("working store link")

    availability_status = _clean_text(
        game.get("availability_status")
    ).casefold()

    if not availability_status:
        add("library status")

    if (
        availability_status == "coming_soon"
        and not _clean_text(game.get("release_date"))
    ):
        add("release date")

    if not genres_valid:
        add("genres (invalid saved data)")
    elif not genres:
        add("genres")

    if not game_modes_valid:
        add("game modes (invalid saved data)")
    elif not game_modes:
        add("game modes")

    if not support_valid:
        add("multiplayer support (invalid saved data)")

    max_players = _positive_int(
        game.get("max_players")
    )
    variable_capacity = bool(
        support.get("mmo")
        or support.get("variable_capacity")
    )
    capacity_tba = bool(
        support.get("capacity_tba")
    )
    online_support = _has_support_value(
        support,
        ONLINE_SUPPORT_FIELDS,
    )
    local_support = _has_support_value(
        support,
        LOCAL_SUPPORT_FIELDS,
    )
    normalised_modes = {
        _clean_text(mode).casefold()
        for mode in game_modes
        if _clean_text(mode)
    }
    known_single_player = (
        max_players == 1
        or (
            "single player" in normalised_modes
            and not any(
                mode in normalised_modes
                for mode in (
                    "multiplayer",
                    "co-operative",
                    "massively multiplayer online (mmo)",
                )
            )
        )
    )

    if (
        not online_support
        and not local_support
        and not variable_capacity
        and not known_single_player
    ):
        add("multiplayer support")

    if (
        max_players is None
        and not variable_capacity
        and not capacity_tba
    ):
        add("overall player limit")

    if (
        max_players is not None
        and not _clean_text(
            game.get("max_players_source")
        )
    ):
        add("player-limit source")

    if (
        support.get("online_coop")
        and _positive_int(
            support.get("online_coop_max")
        ) is None
        and not variable_capacity
        and not capacity_tba
    ):
        add("online co-op limit")

    if (
        support.get("online_multiplayer")
        and _positive_int(
            support.get("online_max")
        ) is None
        and not variable_capacity
        and not capacity_tba
    ):
        add("online multiplayer limit")

    return missing


def audit_game_metadata(game: dict) -> dict:
    support, support_valid = _load_saved_json(
        game.get("multiplayer_support_json"),
        dict,
    )
    genres, genres_valid = _load_saved_json(
        game.get("genres_json"),
        list,
    )
    game_modes, game_modes_valid = _load_saved_json(
        game.get("game_modes_json"),
        list,
    )
    missing = _missing_game_metadata(
        game,
        support=support,
        support_valid=support_valid,
        genres=genres,
        genres_valid=genres_valid,
        game_modes=game_modes,
        game_modes_valid=game_modes_valid,
    )

    return {
        "missing": missing,
        "support": support,
        "genres": genres,
        "game_modes": game_modes,
    }


def build_metadata_audit_report(
    games: list[dict],
) -> tuple[str, dict]:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=REPORT_COLUMNS,
        lineterminator="\n",
    )
    writer.writeheader()
    gap_counts = Counter()
    complete = 0

    for game in games:
        audit = audit_game_metadata(game)
        missing = audit["missing"]

        if missing:
            gap_counts.update(missing)
        else:
            complete += 1

        max_players = _positive_int(
            game.get("max_players")
        )
        support = audit["support"]
        variable_capacity = bool(
            support.get("mmo")
            or support.get("variable_capacity")
        )
        capacity_tba = bool(
            support.get("capacity_tba")
        )
        writer.writerow(
            {
                "Game": _clean_text(
                    game.get("name")
                ) or "Unknown game",
                "Store": _clean_text(
                    game.get("store")
                ) or "Missing",
                "Library section": _clean_text(
                    game.get("library_section")
                ) or "Unclassified",
                "Missing metadata": (
                    "; ".join(missing)
                    if missing
                    else "Complete"
                ),
                "Max players": (
                    max_players
                    if max_players is not None
                    else (
                        "Varies by game/server"
                        if variable_capacity
                        else (
                            "Not announced"
                            if capacity_tba
                            else "Not saved"
                        )
                    )
                ),
                "Player source": _clean_text(
                    game.get("max_players_source")
                ) or "Not saved",
                "Multiplayer support": (
                    _multiplayer_support_summary(
                        audit["support"]
                    )
                ),
                "Genres": " | ".join(
                    _clean_text(value)
                    for value in audit["genres"]
                    if _clean_text(value)
                ) or "Not saved",
                "Game modes": " | ".join(
                    _clean_text(value)
                    for value in audit["game_modes"]
                    if _clean_text(value)
                ) or "Not saved",
                "IGDB ID": (
                    _positive_int(game.get("igdb_id"))
                    or "Not saved"
                ),
                "External ID": _clean_text(
                    game.get("external_id")
                ) or "Not saved",
                "Artwork": (
                    "Saved"
                    if _clean_text(game.get("image_url"))
                    else "Missing"
                ),
                "Link status": _clean_text(
                    game.get("link_status")
                ) or "Unknown",
                "Store link": _clean_text(
                    game.get("store_link")
                ) or "Not saved",
            }
        )

    total = len(games)
    summary = {
        "total": total,
        "complete": complete,
        "incomplete": total - complete,
        "gap_counts": gap_counts,
    }
    return output.getvalue(), summary
