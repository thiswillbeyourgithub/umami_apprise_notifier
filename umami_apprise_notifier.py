#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "umami-analytics",
#     "apprise",
#     "click",
#     "platformdirs",
#     "loguru",
#     "httpx",
# ]
# ///
"""Umami Apprise Notifier — periodically checks Umami analytics for recent
visitors and sends notifications via Apprise.

Intended to be run on a schedule (e.g. cron, systemd timer). Each run queries
the Umami API for visitor activity since the *last successful check* (falling
back to ``--since`` minutes on first run). If any visitors are found, a detailed
notification is dispatched to all configured Apprise targets, including
breakdowns by page, country, city, OS, browser, device, and referrer.

The breakdown data is fetched via the Umami reports API
(POST /api/reports/breakdown) because the umami-analytics Python library only
exposes aggregate stats. We reuse the library's auth token and make direct
httpx calls for the breakdown endpoint.

State (last-check timestamp) is persisted under a platformdirs-managed data
directory so that consecutive runs don't produce overlapping query windows —
even if ``--since`` is larger than the actual run interval.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import apprise
import click
import httpx
import umami
import umami.impl as _umami_impl
from loguru import logger
from platformdirs import user_data_dir

# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

APP_NAME = "umami-apprise-notifier"

# State directory lives under the platform-appropriate user data path
# (e.g. ~/.local/share/umami-apprise-notifier/ on Linux).
STATE_DIR = Path(user_data_dir(appname=APP_NAME))

# A small JSON file that records when the last successful check happened,
# keyed by website_id so multiple sites can be monitored independently.
STATE_FILE = STATE_DIR / "state.json"


def _load_state() -> dict:
    """Load the full state dict from disk.

    Returns
    -------
    dict
        Mapping of ``website_id`` → ``{"last_check_utc": <iso8601>}``.
        Returns an empty dict when no prior state exists or the file is
        corrupt.
    """
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Corrupt state file, starting fresh: {}", exc)
        return {}


def _load_last_check(*, website_id: str) -> datetime | None:
    """Return the last-check UTC timestamp for *website_id*, or ``None``.

    Parameters
    ----------
    website_id : str
        The Umami website ID whose last-check time we want.

    Returns
    -------
    datetime | None
        Timezone-aware UTC datetime, or ``None`` if no record exists.
    """
    data = _load_state()
    entry = data.get(website_id)
    if entry is None:
        return None
    try:
        ts = datetime.fromisoformat(entry["last_check_utc"])
        # Ensure timezone-aware (old state files may lack tzinfo)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    except (KeyError, ValueError) as exc:
        logger.warning("Bad state entry for {}: {}", website_id, exc)
        return None


def _save_last_check(*, website_id: str, timestamp: datetime) -> None:
    """Persist the last-check timestamp for *website_id*.

    Parameters
    ----------
    website_id : str
        The Umami website ID.
    timestamp : datetime
        UTC timestamp to record.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    data = _load_state()
    data[website_id] = {"last_check_utc": timestamp.isoformat()}
    STATE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    logger.debug("Saved last-check for {}: {}", website_id, timestamp.isoformat())


# ---------------------------------------------------------------------------
# Breakdown reports — direct API calls to POST /api/reports/breakdown
# ---------------------------------------------------------------------------

# Each tuple: (api_field_name, display_label, metric_to_show).
# "views" is used for pages because the same visitor can hit multiple pages;
# "visitors" is used for all other dimensions since we care about unique people.
_BREAKDOWN_FIELDS: list[tuple[str, str, str]] = [
    ("path", "Pages", "views"),
    ("referrer", "Referrers", "visitors"),
    ("country", "Countries", "visitors"),
    ("city", "Cities", "visitors"),
    ("os", "Operating Systems", "visitors"),
    ("browser", "Browsers", "visitors"),
    ("device", "Devices", "visitors"),
]

# Cap per category to keep notifications readable on mobile/Telegram.
_MAX_BREAKDOWN_ITEMS = 5


def _fetch_breakdown(
    *,
    website_id: str,
    field: str,
    start_at: datetime,
    end_at: datetime,
) -> list[dict]:
    """Fetch a single breakdown dimension from the Umami reports API.

    Uses the auth token already stored by ``umami.login()`` in the
    ``umami.impl`` module to call ``POST /api/reports/breakdown`` with
    one field at a time, keeping each result set clean (no cross-product
    of multiple dimensions).

    Parameters
    ----------
    website_id : str
        The Umami website ID.
    field : str
        Breakdown field name (e.g. ``"path"``, ``"country"``, ``"os"``).
    start_at : datetime
        Start of the query window (UTC).
    end_at : datetime
        End of the query window (UTC).

    Returns
    -------
    list[dict]
        Raw breakdown rows from Umami, each containing the field value
        plus metrics like ``visitors``, ``views``, etc.
    """
    url = f"{_umami_impl.url_base}/api/reports/breakdown"
    headers = {
        "Authorization": f"Bearer {_umami_impl.auth_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "websiteId": website_id,
        "type": "breakdown",
        "filters": {},
        "parameters": {
            "startDate": start_at.isoformat(),
            "endDate": end_at.isoformat(),
            "fields": [field],
        },
    }
    resp = httpx.post(url, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _fetch_all_breakdowns(
    *,
    website_id: str,
    start_at: datetime,
    end_at: datetime,
) -> dict[str, list[dict]]:
    """Fetch every breakdown dimension for the given time window.

    Iterates over ``_BREAKDOWN_FIELDS`` and collects results. Failures on
    individual dimensions are logged but do *not* prevent other dimensions
    from being fetched — the notification will simply omit that section.

    Parameters
    ----------
    website_id : str
        The Umami website ID.
    start_at : datetime
        Start of the query window (UTC).
    end_at : datetime
        End of the query window (UTC).

    Returns
    -------
    dict[str, list[dict]]
        Mapping of field name to list of breakdown rows.
    """
    results: dict[str, list[dict]] = {}
    for field, label, _metric in _BREAKDOWN_FIELDS:
        try:
            rows = _fetch_breakdown(
                website_id=website_id,
                field=field,
                start_at=start_at,
                end_at=end_at,
            )
            results[field] = rows
            logger.debug("Breakdown '{}': {} row(s)", field, len(rows))
        except Exception as exc:
            logger.warning("Failed to fetch breakdown '{}': {}", field, exc)
            results[field] = []
    return results


def _format_breakdown_section(
    *,
    label: str,
    rows: list[dict],
    field: str,
    metric: str = "visitors",
) -> str | None:
    """Format one breakdown dimension into a single notification text line.

    Sorts rows by *metric* descending, keeps the top
    ``_MAX_BREAKDOWN_ITEMS``, and returns a compact string like
    ``"Pages: / (3), /about (2)"``.  Returns ``None`` when there are no
    rows so the caller can skip the section entirely.

    Parameters
    ----------
    label : str
        Human-friendly section header (e.g. ``"Pages"``, ``"Countries"``).
    rows : list[dict]
        Breakdown rows from the API.
    field : str
        The key in each row holding the dimension value
        (e.g. ``"path"``, ``"country"``).
    metric : str
        Which numeric metric to sort by and display (default
        ``"visitors"``).

    Returns
    -------
    str | None
        Formatted line, or ``None`` if *rows* is empty.
    """
    if not rows:
        return None

    sorted_rows = sorted(rows, key=lambda r: r.get(metric, 0), reverse=True)
    top = sorted_rows[:_MAX_BREAKDOWN_ITEMS]

    items: list[str] = []
    for row in top:
        name = row.get(field, "(unknown)")
        # An empty referrer string means direct / no-referrer traffic.
        if not name:
            name = "(direct)"
        count = row.get(metric, 0)
        items.append(f"{name} ({count})")

    remaining = len(sorted_rows) - len(top)
    if remaining > 0:
        items.append(f"... +{remaining} more")

    return f"{label}: {', '.join(items)}"


def _build_notification_body(
    *,
    stats: object,
    breakdowns: dict[str, list[dict]],
    start_at: datetime,
    now: datetime,
) -> str:
    """Assemble the full notification body from stats and breakdowns.

    The first line is the aggregate summary (visitors, pageviews, visits,
    time window).  Subsequent lines are one per breakdown dimension that
    has data.

    Parameters
    ----------
    stats : object
        The ``WebsiteStats`` object from ``umami.website_stats()``.
    breakdowns : dict[str, list[dict]]
        Result of ``_fetch_all_breakdowns()``.
    start_at : datetime
        Start of the query window.
    now : datetime
        End of the query window.

    Returns
    -------
    str
        Multi-line notification body.
    """
    lines: list[str] = [
        (
            f"{stats.visitors} unique visitor(s), "
            f"{stats.pageviews} pageview(s), "
            f"{stats.visits} visit(s) "
            f"between {start_at.astimezone().strftime('%H:%M')}"
            f" and {now.astimezone().strftime('%H:%M %Z')}."
        ),
        "",  # blank separator
    ]

    for field, label, metric in _BREAKDOWN_FIELDS:
        section = _format_breakdown_section(
            label=label,
            rows=breakdowns.get(field, []),
            field=field,
            metric=metric,
        )
        if section:
            lines.append(section)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


@click.command()
@click.option(
    "--umami-url",
    required=True,
    envvar="UMAMI_URL",
    help="Base URL of your Umami instance (e.g. https://analytics.example.com).",
)
@click.option(
    "--umami-user",
    required=True,
    envvar="UMAMI_USER",
    help="Umami username for API authentication.",
)
@click.option(
    "--umami-password",
    required=True,
    envvar="UMAMI_PASSWORD",
    help="Umami password for API authentication.",
)
@click.option(
    "--website-id",
    required=True,
    envvar="UMAMI_WEBSITE_ID",
    help="Umami website ID to monitor.",
)
@click.option(
    "--since",
    type=int,
    default=5,
    show_default=True,
    envvar="UMAMI_SINCE",
    help="Lookback window in minutes (used on first run or as max window).",
)
@click.option(
    "--apprise-url",
    required=True,
    multiple=True,
    envvar="APPRISE_URL",
    help="Apprise notification URL(s). Can be repeated for multiple targets.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    envvar="UMAMI_DRY_RUN",
    help="Check stats but don't send notifications.",
)
@click.option(
    "--verbose",
    is_flag=True,
    default=False,
    envvar="UMAMI_VERBOSE",
    help="Enable debug logging.",
)
def main(
    umami_url: str,
    umami_user: str,
    umami_password: str,
    website_id: str,
    since: int,
    apprise_url: tuple[str, ...],
    dry_run: bool,
    verbose: bool,
) -> None:
    """Check Umami for recent visitors and notify via Apprise.

    Queries the Umami analytics API for visitor activity in a recent time
    window.  If any visitors are detected, sends a notification to all
    configured Apprise targets.

    The query window starts from the *later* of (a) the stored last-check
    timestamp, or (b) now minus ``--since`` minutes.  This avoids duplicate
    notifications when the script runs more frequently than ``--since``.

    All options can also be set via environment variables (see ``--help``).
    """
    # -- Logging -----------------------------------------------------------
    logger.remove()
    logger.add(sys.stderr, level="DEBUG" if verbose else "INFO")

    # -- Time window -------------------------------------------------------
    now = datetime.now(tz=timezone.utc)
    fallback_start = now - timedelta(minutes=since)

    # Use the stored last-check if it's more recent than the --since
    # fallback, so we never re-query an already-covered period.
    last_check = _load_last_check(website_id=website_id)
    if last_check is not None:
        start_at = max(last_check, fallback_start)
        logger.debug("Last check at {}, using start_at={}", last_check, start_at)
    else:
        start_at = fallback_start
        logger.debug("No prior state, falling back to --since={} min", since)

    # -- Authenticate ------------------------------------------------------
    logger.debug("Connecting to Umami at {}", umami_url)
    try:
        umami.set_url_base(umami_url)
        umami.login(umami_user, umami_password)
    except Exception as exc:
        logger.error("Umami authentication failed: {}", exc)
        sys.exit(1)

    # -- Fetch stats -------------------------------------------------------
    logger.debug(
        "Querying stats for website {} from {} to {}", website_id, start_at, now
    )
    try:
        stats = umami.website_stats(
            start_at=start_at,
            end_at=now,
            website_id=website_id,
        )
    except Exception as exc:
        logger.error("Failed to fetch website stats: {}", exc)
        sys.exit(1)

    logger.info(
        "Stats: {} visitor(s), {} pageview(s), {} visit(s)  [{} → {}]",
        stats.visitors,
        stats.pageviews,
        stats.visits,
        start_at.astimezone().strftime("%H:%M"),
        now.astimezone().strftime("%H:%M %Z"),
    )

    # Persist timestamp *after* a successful query so the next run picks up
    # exactly where this one left off.
    _save_last_check(website_id=website_id, timestamp=now)

    if stats.visitors == 0:
        logger.info("No visitors — skipping notification.")
        return

    # -- Fetch detailed breakdowns -----------------------------------------
    logger.debug("Fetching breakdown reports for the same time window ...")
    breakdowns = _fetch_all_breakdowns(
        website_id=website_id,
        start_at=start_at,
        end_at=now,
    )

    # -- Notify ------------------------------------------------------------
    title = "Umami: visitors detected"
    body = _build_notification_body(
        stats=stats,
        breakdowns=breakdowns,
        start_at=start_at,
        now=now,
    )

    if dry_run:
        logger.info("[DRY RUN] Would send: {} — {}", title, body)
        return

    notifier = apprise.Apprise()
    for url in apprise_url:
        notifier.add(url)

    success = notifier.notify(title=title, body=body)
    if success:
        logger.info("Notification sent successfully.")
    else:
        logger.error("Failed to send notification via Apprise.")
        sys.exit(1)


if __name__ == "__main__":
    main()
