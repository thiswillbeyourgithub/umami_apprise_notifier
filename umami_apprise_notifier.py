#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "umami-analytics",
#     "apprise",
#     "click",
#     "platformdirs",
#     "loguru",
# ]
# ///
"""Umami Apprise Notifier — periodically checks Umami analytics for recent
visitors and sends notifications via Apprise.

Intended to be run on a schedule (e.g. cron, systemd timer). Each run queries
the Umami API for visitor activity since the *last successful check* (falling
back to ``--since`` minutes on first run). If any visitors are found, a summary
notification is dispatched to all configured Apprise targets.

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
import umami
import umami.impl
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
    "--umami-api-key",
    default=None,
    envvar="UMAMI_API_KEY",
    help="Umami API key (preferred over user/password; skips login call).",
)
@click.option(
    "--umami-user",
    default=None,
    envvar="UMAMI_USER",
    help="Umami username for API authentication (ignored when --umami-api-key is set).",
)
@click.option(
    "--umami-password",
    default=None,
    envvar="UMAMI_PASSWORD",
    help="Umami password for API authentication (ignored when --umami-api-key is set).",
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
    umami_api_key: str | None,
    umami_user: str | None,
    umami_password: str | None,
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

    Authentication: supply either ``--umami-api-key`` (preferred) or both
    ``--umami-user`` and ``--umami-password``.  When an API key is provided
    it is injected directly into the library — no login round-trip needed.

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
    # Two modes: API key (preferred, no round-trip) or user/password login.
    # The umami-analytics library doesn't expose an API-key setter, so we
    # inject the key directly into its internal auth_token global — every
    # subsequent call then sends it as ``Authorization: Bearer <key>``.
    logger.debug("Connecting to Umami at {}", umami_url)
    umami.set_url_base(umami_url)

    if umami_api_key:
        logger.debug("Using API key authentication (no login round-trip)")
        umami.impl.auth_token = umami_api_key
    elif umami_user and umami_password:
        logger.debug("Logging in with username/password")
        try:
            umami.login(umami_user, umami_password)
        except Exception as exc:
            logger.error("Umami authentication failed: {}", exc)
            sys.exit(1)
    else:
        logger.error(
            "Supply either --umami-api-key or both --umami-user and --umami-password."
        )
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
        start_at.strftime("%H:%M"),
        now.strftime("%H:%M UTC"),
    )

    # Persist timestamp *after* a successful query so the next run picks up
    # exactly where this one left off.
    _save_last_check(website_id=website_id, timestamp=now)

    if stats.visitors == 0:
        logger.info("No visitors — skipping notification.")
        return

    # -- Notify ------------------------------------------------------------
    title = "Umami: visitors detected"
    body = (
        f"{stats.visitors} unique visitor(s), "
        f"{stats.pageviews} pageview(s), "
        f"{stats.visits} visit(s) "
        f"between {start_at.strftime('%H:%M')} and {now.strftime('%H:%M UTC')}."
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
