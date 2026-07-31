import re
from datetime import datetime

from utils.time_utils import display_now


def release_date_is_future(
    release_date,
) -> bool:
    cleaned_date = str(
        release_date or ""
    ).strip()

    if not cleaned_date:
        return False

    current_date = display_now().replace(
        tzinfo=None
    )

    formats = (
        "%B %Y",
        "%b %Y",
        "%d %B %Y",
        "%d %b %Y",
        "%d %B, %Y",
        "%d %b, %Y",
        "%B %d, %Y",
        "%b %d, %Y",
    )

    for date_format in formats:
        try:
            parsed_date = datetime.strptime(
                cleaned_date,
                date_format,
            )

        except ValueError:
            continue

        if date_format in {
            "%B %Y",
            "%b %Y",
        }:
            return (
                parsed_date.year,
                parsed_date.month,
            ) > (
                current_date.year,
                current_date.month,
            )

        return (
            parsed_date.date()
            > current_date.date()
        )

    year_match = re.fullmatch(
        r"20\d{2}",
        cleaned_date,
    )

    if year_match:
        return (
            int(cleaned_date)
            > current_date.year
        )

    return False


def steam_app_id_from_url(
    store_link,
) -> str | None:
    match = re.search(
        r"store\.steampowered\.com/(?:agecheck/)?app/(\d+)",
        str(store_link or ""),
        re.IGNORECASE,
    )

    if not match:
        return None

    return match.group(1)


def release_info_from_embeds(
    embeds,
    store_link,
) -> dict:
    target_app_id = steam_app_id_from_url(
        store_link
    )

    matching_embeds = []

    for embed in embeds or []:
        embed_url = str(
            getattr(
                embed,
                "url",
                "",
            )
            or ""
        )

        embed_app_id = steam_app_id_from_url(
            embed_url
        )

        if (
            target_app_id
            and embed_app_id == target_app_id
        ):
            matching_embeds.insert(
                0,
                embed,
            )

        else:
            matching_embeds.append(
                embed
            )

    for embed in matching_embeds:
        release_date = None

        for field in getattr(
            embed,
            "fields",
            [],
        ):
            field_name = str(
                getattr(
                    field,
                    "name",
                    "",
                )
                or ""
            ).strip().casefold()

            if field_name in {
                "release date",
                "planned release date",
                "release",
            }:
                release_date = str(
                    getattr(
                        field,
                        "value",
                        "",
                    )
                    or ""
                ).strip()

                if release_date:
                    break

        combined_text = " ".join(
            value
            for value in (
                str(
                    getattr(
                        embed,
                        "title",
                        "",
                    )
                    or ""
                ),
                str(
                    getattr(
                        embed,
                        "description",
                        "",
                    )
                    or ""
                ),
                release_date or "",
            )
            if value
        ).casefold()

        coming_soon = any(
            phrase in combined_text
            for phrase in (
                "planned release",
                "coming soon",
                "not yet available",
            )
        )

        if (
            not coming_soon
            and release_date_is_future(
                release_date
            )
        ):
            coming_soon = True

        if coming_soon or release_date:
            return {
                "coming_soon": coming_soon,
                "release_date": (
                    release_date
                    or None
                ),
            }

    return {
        "coming_soon": False,
        "release_date": None,
    }


def epic_info_from_embeds(
    embeds,
    store_link,
) -> dict:
    target_link = str(
        store_link or ""
    ).casefold()

    matching_embeds = []

    for embed in embeds or []:
        embed_url = str(
            getattr(
                embed,
                "url",
                "",
            )
            or ""
        )

        if (
            target_link
            and embed_url
            and (
                target_link in embed_url.casefold()
                or embed_url.casefold()
                in target_link
            )
        ):
            matching_embeds.insert(
                0,
                embed,
            )

        else:
            matching_embeds.append(
                embed
            )

    for embed in matching_embeds:
        title = str(
            getattr(
                embed,
                "title",
                "",
            )
            or ""
        ).strip()

        if title:
            title = re.sub(
                r"\s*\|\s*Epic Games Store.*$",
                "",
                title,
                flags=re.IGNORECASE,
            ).strip()

        image_url = None

        image = getattr(
            embed,
            "image",
            None,
        )

        thumbnail = getattr(
            embed,
            "thumbnail",
            None,
        )

        for candidate in (
            getattr(image, "url", None),
            getattr(thumbnail, "url", None),
        ):
            cleaned = str(
                candidate or ""
            ).strip()

            if cleaned:
                image_url = cleaned
                break

        if title or image_url:
            return {
                "name": title or None,
                "image_url": image_url,
            }

    return {
        "name": None,
        "image_url": None,
    }


def reconcile_suggestion_metadata(
    game_info: dict,
    *,
    embeds,
    store_link,
    existing_record: dict | None = None,
) -> dict:
    merged = dict(
        game_info
    )

    release_info = release_info_from_embeds(
        embeds,
        merged.get("store_link")
        or store_link,
    )

    if (
        release_info["coming_soon"]
        and not merged.get(
            "availability_verified",
            False,
        )
    ):
        merged["coming_soon"] = True
        merged["availability_status"] = (
            "coming_soon"
        )
        merged["availability_verified"] = True

    if (
        not merged.get("release_date")
        and release_info["release_date"]
    ):
        merged["release_date"] = (
            release_info["release_date"]
        )

    if (
        merged.get("store")
        != "Epic Games Store"
        or merged.get("verification_status")
        not in {
            "blocked",
            "unverified",
        }
    ):
        return merged

    embed_info = epic_info_from_embeds(
        embeds,
        store_link,
    )

    if embed_info.get("name"):
        merged["name"] = embed_info["name"]

    elif existing_record:
        merged["name"] = (
            existing_record.get("name")
            or merged.get("name")
        )

    # A current provider image (including SteamGridDB) is
    # preferred over Discord's preview and an older database URL.
    if not merged.get("image_url"):
        merged["image_url"] = (
            embed_info.get("image_url")
            or (
                existing_record.get("image_url")
                if existing_record
                else None
            )
        )

    if existing_record:
        merged["store_link"] = (
            existing_record.get("store_link")
            or merged.get("store_link")
        )
        merged["link_status"] = (
            existing_record.get("link_status")
            or "live"
        )
        merged["http_status"] = (
            existing_record.get("http_status")
        )
        merged["availability_status"] = (
            existing_record.get(
                "availability_status"
            )
            or merged.get("availability_status")
        )
        merged["release_date"] = (
            existing_record.get("release_date")
            or merged.get("release_date")
        )
        merged["coming_soon"] = bool(
            existing_record.get("coming_soon")
        )
        merged["max_players"] = (
            existing_record.get("max_players")
            or merged.get("max_players")
        )
        merged["max_players_source"] = (
            existing_record.get(
                "max_players_source"
            )
            or merged.get(
                "max_players_source"
            )
        )

    elif (
        embed_info.get("name")
        or embed_info.get("image_url")
    ):
        merged["link_status"] = "live"

    return merged
