## Command notification/read-state fix

- Uses the working `/3/larynx/history` source for command mentions.
- Calls `POST /3/notification/{id}` for unseen command-bearing notifications.
- Posts threaded command replies with the known-working JSON body on `POST /3/comment`.
- `DRY_RUN` now suppresses only automatic repost-statistics comments; explicit `blockme` and `help` replies remain live.
- Local notification IDs still prevent duplicate command execution even if Imgur does not immediately update remote read state.

# Upgrade notes

This build adds the LAN administration console on top of the previous `blockme` build.

## Keep these files

Do not delete or replace your populated SQLite database or your configured `.env` when upgrading.

```text
repoststats.sqlite3
.env
```

The application automatically creates the new tables required for:

- runtime settings
- web-managed jobs
- repost-event history
- Imgur API telemetry

Existing `posts`, block lists, and processed notification data remain compatible.

## New dependencies

Run after replacing the project files:

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

This adds Flask and Waitress.

## Start the web UI

```bash
python app.py web
```

Open `http://<server-lan-ip>:8080`.

There is intentionally no application login. Keep TCP/8080 restricted to your LAN and do not port-forward it.

## Existing backfill

If a backfill is already running from an older copy, let it finish or stop it before replacing the code. Web progress tracking only applies to jobs started from the new web UI; existing indexed rows remain usable.

## Historical dashboard data

`repost_events` and `api_events` begin recording when this build runs. Existing indexed posts are not reprocessed merely to reconstruct old event/telemetry history.

## 2026-08-19 command-history diagnostics

- Fixed command polling treating an Imgur `data: null` history response as an empty history.
- Command polling now verifies that `IMGUR_ACCESS_TOKEN` belongs to `BOT_USERNAME` before polling private account history.
- The web-history endpoint is tried with `IMGURPLATFORM=web` as both a query parameter and a request header.
- Added fallback to Imgur's documented `/account/{username}/notifications/replies?new=false` endpoint.
- Added `python app.py debug-history`, which reports OAuth account identity and endpoint response shapes/counts/types without printing tokens or comment bodies.

## 2026-08-19: Imgur command-history fix

Command polling now uses Imgur's literal authenticated `/3/larynx/history` route. `larynx` is an internal route name, not the bot username. The OAuth bearer token determines which account history is returned. The earlier `/{BOT_USERNAME}/history` probes returned `data: null` and could not see `commentmention` events.

`python app.py debug-history` now probes `larynx-history` first and keeps the v3 reply-notifications endpoint only as a comparison diagnostic.

## Web worker restart reconciliation

The web console intentionally launches bot/backfill workers in their own process session, so restarting only the web console does not stop an active worker. On startup/request reconciliation, the console now verifies that a persisted PID belongs to the expected `app.py --job-id N` worker instead of treating any reused PID as proof the job is still running. A verified surviving worker remains active and can still be stopped from the restarted dashboard; stale/reused PIDs are marked `exited`.
