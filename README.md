# Umami Apprise Notifier

A small Python script that periodically checks [Umami](https://umami.is/) analytics for recent visitors and sends detailed notifications via [Apprise](https://github.com/caronc/apprise) (Telegram, Slack, email, 90+ services).

Designed to be run on a schedule (cron, systemd timer). Each run queries the Umami API for the time window since the last check, and fires a notification only when visitors are detected — no spam, no overlapping windows. Notifications include breakdowns by page, referrer, country, city, OS, browser, and device via the Umami [reports API](https://umami.is/docs/api/reports).

## License

AGPLv3 — see `LICENSE` file.

## Requirements

- Python ≥ 3.10
- [`uv`](https://github.com/astral-sh/uv) (dependencies are managed inline via [PEP 723](https://peps.python.org/pep-0723/) — no virtualenv or `pip install` needed)

## Usage

```bash
# Make executable (once)
chmod +x umami_apprise_notifier.py

# Run directly — uv resolves deps automatically on first invocation
./umami_apprise_notifier.py \
    --umami-url https://analytics.example.com \
    --umami-user admin \
    --umami-password changeme \
    --website-id xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx \
    --since 5 \
    --apprise-url "tgram://bottoken/ChatID"
```

### All options

| Option              | Env var              | Description                                               |
|---------------------|----------------------|-----------------------------------------------------------|
| `--umami-url`       | `UMAMI_URL`          | Base URL of your Umami instance                           |
| `--umami-user`      | `UMAMI_USER`         | Umami API username                                        |
| `--umami-password`  | `UMAMI_PASSWORD`     | Umami API password                                        |
| `--website-id`      | `UMAMI_WEBSITE_ID`   | Website ID to monitor                                     |
| `--since`           | `UMAMI_SINCE`        | Lookback window in minutes (default: 5, used on 1st run)  |
| `--apprise-url`     | `APPRISE_URL`        | Apprise URL(s) — repeatable for multiple targets          |
| `--dry-run`         | `UMAMI_DRY_RUN`      | Check stats without sending notifications                 |
| `--verbose`         | `UMAMI_VERBOSE`      | Enable debug logging                                      |

Options can be passed as CLI arguments, environment variables, or a mix of both.

### State

A small JSON file is stored under `~/.local/share/umami-apprise-notifier/` (Linux, via [`platformdirs`](https://github.com/tox-dev/platformdirs)). It records the last-check timestamp per website ID so consecutive runs query non-overlapping time windows.

## Scheduling

### Crontab

```bash
# Check every 5 minutes
crontab -e
```

```cron
*/5 * * * * /full/path/to/umami_apprise_notifier.py --umami-url https://analytics.example.com --umami-user admin --umami-password changeme --website-id xxxxxxxx --since 5 --apprise-url "tgram://bottoken/ChatID"
```

Or, if you prefer environment variables to avoid long lines:

```cron
UMAMI_URL=https://analytics.example.com
UMAMI_USER=admin
UMAMI_PASSWORD=changeme
UMAMI_WEBSITE_ID=xxxxxxxx
UMAMI_SINCE=5
APPRISE_URL=tgram://bottoken/ChatID

*/5 * * * * /full/path/to/umami_apprise_notifier.py
```

> Make sure `uv` is on `PATH` in the cron environment (use the full path to `uv` in the shebang if needed).

### systemd (recommended)

Service and timer unit files are provided in `systemd/`.

```bash
# 1. Copy the env file and fill in your credentials
sudo cp systemd/umami-apprise-notifier.env.example /etc/umami-apprise-notifier.env
sudo chmod 600 /etc/umami-apprise-notifier.env
sudo nano /etc/umami-apprise-notifier.env

# 2. Copy the script to a known location
cp umami_apprise_notifier.py ~/.local/bin/umami_apprise_notifier.py
chmod +x ~/.local/bin/umami_apprise_notifier.py

# 3. Install the systemd units (user-level)
mkdir -p ~/.config/systemd/user/
cp systemd/umami-apprise-notifier.service ~/.config/systemd/user/
cp systemd/umami-apprise-notifier.timer ~/.config/systemd/user/

# 4. Enable and start
systemctl --user daemon-reload
systemctl --user enable --now umami-apprise-notifier.timer

# 5. Verify
systemctl --user status umami-apprise-notifier.timer
systemctl --user list-timers
```

To check logs:

```bash
journalctl --user -u umami-apprise-notifier.service -f
```

## How it works

```
 ┌──────────┐         ┌──────────┐  stats  ┌──────────┐
 │  Timer / │  runs   │  Script  │────────>│  Umami   │
 │  Cron    │────────>│          │<────────│  API     │
 └──────────┘         │          │         │          │
                      │ visitors │         │          │
                      │   > 0 ?  │ reports │          │
                      │    │     │────────>│          │
                      │   yes    │<────────│          │
                      │    │     │         └──────────┘
                      │    │     │
                      │    │     │         ┌──────────┐
                      │    └─────│────────>│ Apprise  │──> Telegram, Slack, …
                      │          │ notify  └──────────┘
                      └──────────┘
```

The notification includes a detailed breakdown:

```
Umami: visitors detected

3 unique visitor(s), 5 pageview(s), 4 visit(s) between 10:00 and 10:05 UTC.

Pages: / (3), /about (2)
Referrers: google.com (2), (direct) (1)
Countries: US (2), DE (1)
Cities: Berlin (1), New York (1)
Operating Systems: Mac OS (2), Windows 10 (1)
Browsers: Chrome (2), Firefox (1)
Devices: Desktop (2), Mobile (1)
```

---

*Built with [Claude Code](https://claude.ai/claude-code).*
