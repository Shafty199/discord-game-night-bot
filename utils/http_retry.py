import asyncio
import logging
import random
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import aiohttp


LOGGER = logging.getLogger(__name__)

RETRYABLE_HTTP_STATUSES = frozenset(
    {
        429,
        502,
        503,
        504,
    }
)
DEFAULT_ATTEMPTS = 3
MAX_RETRY_DELAY_SECONDS = 5.0


def _retry_after_seconds(response) -> float | None:
    headers = getattr(response, "headers", None)

    if not headers:
        return None

    raw_value = headers.get("Retry-After")

    if raw_value is None:
        return None

    try:
        return max(
            0.0,
            min(
                float(raw_value),
                MAX_RETRY_DELAY_SECONDS,
            ),
        )

    except (TypeError, ValueError):
        pass

    try:
        retry_time = parsedate_to_datetime(
            str(raw_value)
        )

        if retry_time.tzinfo is None:
            retry_time = retry_time.replace(
                tzinfo=timezone.utc
            )

        remaining = (
            retry_time.astimezone(timezone.utc)
            - datetime.now(timezone.utc)
        ).total_seconds()

        return max(
            0.0,
            min(
                remaining,
                MAX_RETRY_DELAY_SECONDS,
            ),
        )

    except (TypeError, ValueError, OverflowError):
        return None


def _backoff_seconds(
    retry_number: int,
    response=None,
) -> float:
    retry_after = (
        _retry_after_seconds(response)
        if response is not None
        else None
    )

    if retry_after is not None:
        return retry_after

    base_delay = min(
        0.35 * (2 ** max(0, retry_number - 1)),
        2.0,
    )
    return min(
        base_delay + random.uniform(0.0, 0.15),
        MAX_RETRY_DELAY_SECONDS,
    )


@asynccontextmanager
async def retrying_request(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    retry_statuses=RETRYABLE_HTTP_STATUSES,
    **request_kwargs,
):
    """Open an HTTP response with two bounded transient retries.

    The caller still owns response parsing and final status handling.
    Only connection failures and explicitly retryable HTTP responses
    are retried, so permanent 4xx failures return immediately.
    """

    request_method = getattr(
        session,
        str(method).strip().casefold(),
    )
    attempt_count = max(1, int(attempts))
    last_error = None

    for attempt_index in range(attempt_count):
        request_context = request_method(
            url,
            **request_kwargs,
        )

        try:
            response = await request_context.__aenter__()

        except (
            asyncio.TimeoutError,
            aiohttp.ClientConnectionError,
        ) as error:
            last_error = error

            if attempt_index >= attempt_count - 1:
                raise

            retry_number = attempt_index + 1
            delay = _backoff_seconds(retry_number)
            LOGGER.debug(
                "Retrying %s %s after %s (%s/%s) in %.2fs",
                method.upper(),
                url,
                type(error).__name__,
                retry_number,
                attempt_count - 1,
                delay,
            )
            await asyncio.sleep(delay)
            continue

        status = int(
            getattr(response, "status", 0) or 0
        )

        if (
            status in retry_statuses
            and attempt_index < attempt_count - 1
        ):
            retry_number = attempt_index + 1
            delay = _backoff_seconds(
                retry_number,
                response,
            )
            await request_context.__aexit__(
                None,
                None,
                None,
            )
            LOGGER.debug(
                "Retrying %s %s after HTTP %s (%s/%s) in %.2fs",
                method.upper(),
                url,
                status,
                retry_number,
                attempt_count - 1,
                delay,
            )
            await asyncio.sleep(delay)
            continue

        try:
            yield response

        except BaseException as error:
            await request_context.__aexit__(
                type(error),
                error,
                error.__traceback__,
            )
            raise

        else:
            await request_context.__aexit__(
                None,
                None,
                None,
            )

        return

    if last_error is not None:
        raise last_error

    raise RuntimeError(
        "HTTP retry loop ended without returning a response."
    )
