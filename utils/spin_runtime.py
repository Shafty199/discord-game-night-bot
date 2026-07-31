import asyncio
from collections.abc import Awaitable

from utils.store import get_steam_sale_info


async def animate_with_sale_lookup(
    animation: Awaitable,
    *,
    session,
    game,
) -> dict | None:
    """Run the animation and sale lookup at the same time."""
    sale_info_task = asyncio.create_task(
        get_steam_sale_info(
            session=session,
            store_link=game[2],
            store=game[3],
        )
    )

    try:
        await animation
        return await sale_info_task

    except BaseException:
        if not sale_info_task.done():
            sale_info_task.cancel()

        await asyncio.gather(
            sale_info_task,
            return_exceptions=True,
        )
        raise
