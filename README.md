# RepostStatisticsRedux

A Python 3 modernization of SleepProgger's RepostStatistics idea: crawl Imgur gallery posts, fingerprint media, remember history in SQLite, detect reposts, and optionally comment when a previously seen image appears again.

Original source: https://github.com/SleepProgger/RepostStatistics

## Core behavior

- `DRY_RUN=true` is the default. Reposts are detected and stored but no automatic repost comments are published. Explicit `@bot` command replies remain enabled.
- `backfill` can never post comments, regardless of `DRY_RUN`.
- Static images use 256-bit aHash plus horizontal/vertical dHash perceptual matching.
- Animated media is currently matched conservatively by exact downloaded bytes.
- Albums are skipped, matching the original bot's basic behavior.
- SQLite is used for image history, blocks, command deduplication, web settings, job state, repost-event history, and API telemetry.
- Imgur 500/502/503/504 responses are retried with exponential backoff. A page that still fails is logged and the crawl continues.
- Users can opt out with `@RepostStatisticsRedux blockme`.

## Install / upgrade

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

On an upgrade, keep your existing `.env` and `repoststats.sqlite3`. The new SQLite tables are created automatically; do not delete the database you already backfilled.

At minimum, set:

```dotenv
IMGUR_CLIENT_ID=...
DATABASE_PATH=repoststats.sqlite3
```

To post comments and process account mention commands, configure the bot account's OAuth values as well.

## Backfill

```bash
python app.py backfill --section top --window all --pages 5000
```

You can resume at an explicit page:

```bash
python app.py backfill --section top --window all --start-page 5000 --pages 5000
```

Backfill never comments.

## Live bot

One scan:

```bash
python app.py run --once
```

Continuous:

```bash
python app.py run
```

Check database counts:

```bash
python app.py stats
```

## LAN admin web console

The project now includes a small Flask admin app served by Waitress. It intentionally has no login because it is designed for a trusted homelab/LAN. Do not expose or port-forward it to the public internet.

Start it:

```bash
python app.py web
```

Defaults:

```dotenv
WEB_HOST=0.0.0.0
WEB_PORT=8080
```

Then open:

```text
http://<server-lan-ip>:8080
```

The web console includes:

- dashboard counts and worker state
- start/stop for the live bot
- start/stop backfills, including start page and page count
- backfill progress and per-job logs
- username and numeric account-ID blocklist management
- recent repost detections and decision reasons
- recently indexed posts
- Imgur API response/rate-limit telemetry and 503 counts
- live operational settings stored as SQLite overrides
- main bot log viewer

Secrets such as `IMGUR_CLIENT_SECRET`, access tokens, and refresh tokens are never editable from the web UI; they remain in `.env`.

Runtime settings saved in the web UI take effect the next time a bot/backfill worker starts. If the live bot is already running, stop and start it from the dashboard after changing settings.

### Firewall example

If the server uses UFW and your LAN is `192.168.1.0/24`:

```bash
sudo ufw allow from 192.168.1.0/24 to any port 8080 proto tcp
```

Adjust the subnet to your actual LAN.

## `blockme`

Authenticated command polling watches the bot account's mention/reply history. A user can opt out with:

```text
@RepostStatisticsRedux blockme
```

The command is strictly self-service. The notification author is blocked; text after `blockme` cannot be used to block another account. When possible, both the username and stable numeric Imgur account ID are stored. Command-bearing notifications are also marked viewed through Imgur after they are seen. `DRY_RUN=true` does not suppress `blockme`/`help` replies; it only suppresses automatic repost-statistics comments.

Manual equivalents:

```bash
python app.py blockuser SomeImgurUser
python app.py unblockuser SomeImgurUser
python app.py block 123456
python app.py unblock 123456
```

## Repost-comment rules

Default filters are:

- minimum net score: 40
- current post younger than 3 days
- blocked accounts/users ignored
- same-account reposts ignored
- exact (byte-identical SHA-256) matches always qualify, regardless of the original's age or title
- for perceptual (lookalike) matches: sufficiently similar titles qualify
- otherwise the prior occurrence must be within 60 days and the title cannot be an `mrw`, `mfw`, or `when ...` reaction-style title

The web UI exposes these operational thresholds.

Example comment:

```text
Image last seen 8 months before at https://imgur.com/gallery/OLD_ID Title similarity: ★★★☆☆
```

Perceptual rather than exact matches say `probably last seen`.

## Weekly recap post

When `WEEKLY_POST_ENABLED=true`, the live bot publishes a weekly repost-statistics
recap to the Imgur gallery: one meme template plus the week's numbers in the title
and description. It fires on the configured weekday/time (`WEEKLY_POST_DAY`,
`WEEKLY_POST_TIME`, local), once per week (idempotent).

Templates live in `WEEKLY_TEMPLATE_DIR` (default `template/`). They are picked
least-recently-used, so 52 templates rotate across a year with no repeats; new
files are picked before any already-used one, and deleted files are ignored.
Filenames containing `example` are skipped.

The stats count only what the **live** bot found in the window (backfill
detections are excluded). "Reposts found" is every live detection; "Commented on"
is the subset that got a public comment. The description lists the **original**
posts whose reposts were commented on (never the reposters).

Preview the exact title, description, and chosen template without posting:

```bash
python app.py post-weekly --dry-run
```

Post immediately (respects the once-per-week guard):

```bash
python app.py post-weekly
```

Publishing uses the account's OAuth token via a four-step gallery flow (create
album, upload, share to gallery, set description). If the gallery-share step ever
rejects the token, a browser session cookie can be supplied as a fallback.

## API telemetry

Workers record Imgur API status codes and any `X-RateLimit-*` response headers into a capped local telemetry table. The web console displays the latest client/user remaining values plus 503/5xx/429 counts over the previous 24 hours. Only the newest ~50,000 response records are retained.

## Logs

Main log:

```text
logs/repostbot.log
```

Web-started jobs also get individual logs under:

```text
logs/jobs/
```

## systemd

Example service files are under `systemd/`. The bot and web console are separate processes, so the admin page remains responsive during a large backfill.

## Tests

```bash
python -m pytest -q
```

`pytest` is only needed for development/testing.

### Debugging `blockme` / mention polling

If Imgur shows an @-mention in the website but the bot does not process it, run:

```bash
python app.py debug-history
```

This verifies which Imgur account the OAuth access token belongs to and probes the known history/notification endpoints. It intentionally prints only response shapes, counts, and event type names; it does not print access tokens or comment bodies.

### Restarting the web console while jobs are running

Jobs started from the web UI run in their own process sessions. Stopping or restarting `python app.py web` does **not** stop an active live bot or backfill. When the web console starts again it verifies the stored PID and adopts the surviving worker. If the PID is gone or has been reused by another process, the old job is automatically marked `exited`.
