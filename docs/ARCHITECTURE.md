# ExposedFX forwarding — architecture and runbook

Last verified: 4 Nov 2026. Keep this file in step with `tests/test_live_route_contract.py`.

## Services (Railway project `brilliant-blessing`, environment `production`)

| Service | Branch | Start command | Account | Volume |
|---|---|---|---|---|
| `imperium-telegram-worker` (MAIN) | `main` | `python -m telegram_worker.worker_fixed` | KratosFX (premium, custom emoji) | `/app/data` |
| `imperium-telegram-worker-alt` (ALT) | `main` | `python -m telegram_worker.alt_worker` | "new account" | `/data-alt` |
| `ExposedFX-Elite` (server) | `main` | `python server/main.py` | none (FastAPI for MT5) | none |
| `exposedfx-clean-signal-forwarder` | `main` | — | — | offline by owner |
| `exposedfx-ai-signal-formatter` | `main` | — | — | offline by owner |

Deploys never overlap (`overlapSeconds=0`), so a session is never used by two
containers at once. One codebase on `main` serves both accounts; Railway watch
paths decide who restarts:

- MAIN: `**` except `telegram_worker/alt_*.py`, `tests/test_alt_*.py` and this
  file (`/**` never matched in Railway's matcher; `**` does).
- ALT: `telegram_worker/alt_*.py`, `telegram_worker/engine/**`,
  `permanent_delivery_errors.py`, `owner_forwarding_policy.py`,
  `health_monitor.py`, `liveness.py`, `log_setup.py`, `__init__.py`,
  `runtime_guard.py`, `requirements.txt` (the only modules ALT imports). Add a
  module here if ALT starts importing it.
- server: `server/**` only (it does not use Telethon; a rebuild would reset its
  autocopier SQLite, which lives on ephemeral disk).

Telethon is pinned to 1.45.0 in `requirements.txt` (the version both workers
run and CI tests); raising it is a deliberate change that rebuilds MAIN and ALT.

The old `alt-worker` branch is frozen as a rollback point (Railway → ALT →
Settings → Source → branch `alt-worker`).

## Flow

```
providers ──ALT──▶ VIP RELAY GROUP (-1004367822325) ──MAIN──▶ live destinations
                         topics 2, 115, 7248, 7250
SamGtrades (-1004281603170) ──MAIN (whole chat, house format)──▶ Exposed | VIP
Market Slayers VIP (-1003838973021) ──MAIN──▶ IMPERIUM FX VIP topic 9
```

Every live route of both workers is defined once, in
`telegram_worker/route_table.py` (typed, validated at start-up: no route can
write into a retired topic or repeat another). The tables below mirror it.

**Add a route: one line in `route_table.py`, then push to `main`.**

| Want | Line to add |
|---|---|
| MAIN copies new posts live | `live("Name", (source_chat, source_topic), (dest_chat, dest_topic)),` |
| MAIN imports the last N posts once, then live | `history("Name", (source_chat, source_topic), (dest_chat, dest_topic), last=20),` |
| ALT copies a provider into a relay topic | `relay("Name", (source_chat, source_topic), relay_topic),` |

`source_topic` `None` means the whole chat. A new route starts at the newest
post (nothing old is sent, except `history`'s last N); the account must already
be in both chats. Filters, formatting, replies, edits, restart recovery and the
replay audit apply automatically. **Remove a route:** delete its line.

### ALT routes (4)
| Route | Source | Relay topic |
|---|---|---|
| ALT RELAY 1 | Private Circle G Trades | 2 |
| ARABIC VIP EN -> RELAY115 | T4T AE Scalping VIP (translated to English) | 115 |
| NewAccount Topic5246 To Relay7248 | Blackline / 5246 | 7248 B+SCALPING |
| NewAccount Topic5247 To Relay7250 | Blackline / 5247 | 7250 B+XAU |

### MAIN routes
| Path | Source | Destination | Format |
|---|---|---|---|
| engine intake | Market Slayers VIP | IMPERIUM FX VIP · ᴍᴀʀᴋᴇᴛ ꜱʟᴀʏᴇʀꜱ ᴠɪᴘ (9) | copy |
| engine intake | relay 106 AKX | VIP 11 | copy |
| engine intake | relay 2 | VIP Extra Partner 2 (1364) | copy |
| engine intake | relay 508 XAU Exclusive Room | VIP 7 | Imperium VIP format |
| engine intake | relay 7248 | VIP EXTRA PARTNER 1 (10877) | copy |
| owner routes (`owner_routes.py`) | relay 7250 | VIP EXTRA PARTNER [ 4 ] (12161) | copy |
| owner routes | VIP 1364 / 10877 / 12161 | ᴍᴀʀᴋᴇᴛ ᴍᴀᴋᴇʀꜱ 28 / 8 / 24 | copy |
| `samgtrades_relay.py` | Signals \| SamGtrades | Exposed \| VIP | house library format |

Formatters: `samgtrades_house_formatter.py` (Exposed | VIP), `imperium_vip_*` (VIP 7).
Custom emoji ids come from the owner's Saved Messages (`community_emoji_registry.py`):
each start re-reads the newest 500; an alias whose assignment has scrolled further
back keeps its saved value (only `Alias = DELETE` removes one).

## Delivery engine (`telegram_worker/engine/`)

Every new post on every route, MAIN and ALT, is delivered by the engine (see
the package docstring). ALT uses `engine/alt.py` (Telegram translation for the
T4T route, ALT reply recovery, `[ALT COPIED]` markers). New posts reach it
through `engine/intake.py` on both workers (below). On MAIN the intake calls
`fast_vip_deliver_route`, which hands the post to `DeliveryEngine.deliver`;
the owner routes call `DeliveryEngine.copy_unit`.
One planner decides Send or Skip (rules, reply target, formatter), one
deliverer sends, verifies, retries and writes the message map. Edits and
deletes go through `engine/mirror.py` (below), which reads the same map.

How it replaced the legacy copy pipeline (4 Nov 2026): shadow mode compared
every live post with the legacy output, a replay audit matched 156/156
delivered posts on the 7 active routes, one route ran on the engine as a
canary, then all routes moved and the legacy copy stack (copy_one,
copy_album, their retry wrappers and the four modules that patched them) was
deleted. The legacy rules are frozen in
`tests/fixtures/legacy_rules_20261104.py` and the engine is tested against
them on generated corpora (`tests/test_engine.py`).

Rollback: Railway, MAIN, Deployments, redeploy the last deployment before
the cutover commit. State files are shared and need no changes.

## Safety layers

1. `owner_forwarding_policy.py` — `STOPPED_DEST_CHATS` / `STOPPED_SOURCE_CHATS`
   remove routes at startup; `install_send_guard` refuses any send, forward or
   edit into a stopped chat on every MAIN code path.
2. Runtime stops (`owner_stopped_forwarding_20261102.json` on the volume) are
   written only for real access loss; "entity/topic unavailable" must repeat
   3 times over 10 minutes first.
3. Paused routes are archived in `docs/paused_routes_20261103.md` (45 routes).
4. CI: `forwarding-safety.yml` compiles both workers, checks route contracts,
   black, pyflakes, strict mypy on the engine and state store, every unit
   test (MAIN and ALT) and a 95% branch-coverage floor on the engine, state
   store, liveness and monitoring, on each push. It also checks that every
   module ALT imports matches ALT's Railway watch paths (`ALT_WATCH_PATHS` in
   `scripts/check_forwarding_contracts.py`; keep both lists identical), so ALT
   never keeps running old code. `.python-version` pins Railway and CI to 3.13.
5. Replay audit: after every deploy each worker re-plans its most recent
   delivered posts on every route and compares them with the copies already sent;
   any difference is a Saved Messages notice (🔍).

**Intake (`engine/intake.py`, MAIN and ALT).** Telegram pushes are the fast
path; each source (chat + topic) has one writer and an oldest-first queue.
Album items are gathered by the intake itself (queued 0.6 s after the last
item), and the writer holds a newer post until an older album is queued, so a
post sent right after an album never overtakes it. A
watchdog per source reads only the posts after its checkpoint every 30 s and
queues anything not handled: a missed push, a failed delivery, or posts that
arrived during a restart. The checkpoint ("everything up to here is handled")
is stored in `message_map.sqlite3` and moved only by the watchdog over posts it
has seen, so a later success can never skip an earlier failure. On MAIN a post
older than 10 minutes is not sent (stale signals mislead) and you get a Saved
Messages notice; ALT has no age limit. A post that still fails an hour after
its first failure is skipped with a notice. Owner routes and the SamGtrades
relay keep their own checkpoints, with the same one-hour
give-up; the SamGtrades relay also catches up posts made while the worker was
down (deploys) and applies the 10-minute rule.

**State store (`state_store.py`).** The message map lives in SQLite
(`message_map.sqlite3`, one row per copy) instead of a JSON file rewritten on
every delivery. `message_map.json` is still written every 5 minutes and when
Railway stops the worker, so rolling back to older code starts from the
current map; entries only the JSON has are merged back on the next start.
On SIGTERM, sends already in flight (intake, owner routes,
SamGtrades relay) get up to 2 s to record their copy and no new send starts,
so a deploy never sends a post twice.

**Edits and deletions (`engine/mirror.py`, MAIN and ALT).** An edited source
post updates each mapped copy in place, rendered like a new post (restyled for
VIP 7, translated on ALT's T4T route); an edit never creates, deletes or
re-sends a copy, and album copies are left alone. MAIN passes the edit on to
copies of that copy (VIP topic to partner topic); ALT never changes a copy's
media structure. A deleted source post deletes its copies on MAIN. A backup
poller re-checks the newest Relay508 posts every 30 s for missed edit events.

**No monkeypatching.** Startup is explicit in `worker_fixed.main()`: engine,
owner routes, SamGtrades relay, health monitor, replay audit,
intake. The one interception left is the owner send guard on
`client._call`, the last line of defence for stopped chats.

## Monitoring

Logs are one JSON object per line on Railway (`log_setup.py`), so the log
explorer shows real severities (`warn` for notices, `error` only for errors)
and `@logger:imperium-worker` style filters work.

`liveness.py` (both workers) restarts a worker that has stopped working
without crashing: if Telegram drops the connection for good, or the event loop
stalls for 120 s, the process exits non-zero so Railway starts a fresh one,
and the next start sends "♻️ … restarted itself" with the reason to that
account's Saved Messages. On MAIN, a background job that crashes (owner
routes, SamGtrades relay, edit reconciler, weekly stats) is
logged, alerted (🚨) and started again after 30 s instead of staying dead.
A flood wait while loading the chat list at start is retried after the wait,
and until then "chat not found" is not counted towards stopping a route. A redeploy no longer waits for the old run's lock:
Railway never runs two copies of a volume-backed service, so the lock left by
an earlier deployment is taken over at once (`runtime_guard.py`).

`health_monitor.py` (both workers) reads each worker's own log lines and
messages that account's Saved Messages only when something needs attention:

| Alert | Meaning | First check |
|---|---|---|
| ⛔ route stopped | Telegram refused access permanently | Is the account still in the destination/source? |
| ⚠️ N failed deliveries in 10 min | Same route failing repeatedly | Railway logs for that route name |
| ⏳ flood wait ≥ 60s | Telegram rate-limited the account | Usually clears itself; repeated = too many API calls |
| 🚨 unexpected error | A traceback outside known paths | Railway logs at that time |
| ⏭️ post(s) not sent | MAIN post older than 10 min when it could be sent (restart, long outage) | Forward manually if still relevant |
| ⚠️ post(s) skipped after N attempts | Delivery kept failing for about an hour | Railway logs for that route; forward manually |
| 🔍 sent differently after this deploy | Replay audit: new code would format recent posts differently | Check the change; roll back if unintended |
| ♻️ restarted itself | Lost connection or stalled loop; restarted automatically | Nothing, unless it repeats |

A digest of deliveries, failures and lag per route arrives daily at 08:00 Malta.

Optional dead-man's switch: set `HEARTBEAT_URL` (for example a
healthchecks.io check) on MAIN and ALT. Each pings it every 60 s while
connected to Telegram, so the check alerts you if a worker is down, stuck or
logged out, even when nothing in Railway looks wrong. Also turn on Railway's
deploy-crash notifications in the account settings.

## State on the volumes (never delete or overwrite)

MAIN `/app/data`: session (rewritten from `SESSION_B64_*` at start),
`message_map.sqlite3` (source→destination ids for replies/edits, plus the
intake checkpoints) and its `message_map.json` snapshot (an unreadable file
is copied to `message_map.corrupt-*.json`), owner route state,
wholechat map, stop ledger, `health_monitor_v1.json`, `engine_shadow_v1.json`,
`liveness_exit_v1.json` (only between a self-restart and the next start).
ALT `/data-alt`: session, `message_map.sqlite3` + JSON snapshot, route checkpoints (`route_state.json`, kept for rollbacks), last-N import state,
`health_monitor_v1.json`, `liveness_exit_v1.json`.

## Runbook

- **Add, remove or restore a route:** one line in `route_table.py` (see
  "Add a route"; old paused definitions are in
  `archive/telegram_worker/routes_before_table_20261104.py`), push to `main`.
  For a paused destination also remove its id from `STOPPED_DEST_CHATS` in
  `owner_forwarding_policy.py`.
- **Resume a route stopped at runtime:** delete its entry from
  `owner_stopped_forwarding_20261102.json` on the volume, then restart.
- **Roll back a deploy:** Railway → service → Deployments → previous → Redeploy.
  State files are forward-compatible; nothing needs undoing.
- **New Telegram session:** generate it with an interactive login on a trusted
  machine, store it only in Railway variables, never in chat or git.
- **Before any change:** `python scripts/check_forwarding_contracts.py`,
  `python -m unittest discover -s tests` and
  `black --check --skip-magic-trailing-comma -l 110 telegram_worker scripts tests`
  must pass (CI also runs pyflakes, mypy and the coverage floor).

## Obsolete MAIN variables (safe to delete in Railway)

No code reads these any more (checked 4 Nov 2026); they belong to retired
one-time jobs, the removed legacy copy path or the shadow-mode switch.
Deleting them changes nothing; Railway restarts MAIN once.

`ALLOW_EMPTY_TOPIC_LAST50_V1`, `ALLOW_MARKET_SLAYERS_LAST50_V1`,
`ALLOW_MARKET_SLAYERS_LAST50_V2`, `ALLOW_ONE_TIME_BACKFILL_3838973021_TO_6813`,
`ALLOW_ONE_TIME_BACKFILL_3893360328_TO_361`, `ALLOW_RELAY2_TO_VIP1364_LAST50_V1`,
`ALLOW_RELAY2_TO_VIP1364_LAST50_V2`, `ALLOW_RELAY2_TO_VIP1364_LAST50_V3`,
`ALLOW_SAFE_PROBE_3229476368_TO_5947`, `AUTO_SYNC_TOPIC_TITLES`, `AUTO_TOKEN`,
`BLOCKED_SENDER_CLEANUP_ENABLED`, `BLOCKED_SENDER_CLEANUP_LIMIT`,
`DEDUP_WINDOW_SECONDS`, `ENABLE_ALBUM_HANDLER`, `ENABLE_ALBUM_REUPLOAD_FALLBACK`,
`ENGINE_ROUTES`, `FAST_TOPIC11_POLL_SECONDS`, `FAST_VIP_POLL_SECONDS`,
`MIRROR_STRUCTURE_REPAIR`, `NEW_MIRROR_BACKFILL_ALL_ON_START`,
`NEW_MIRROR_BACKFILL_LIMIT`, `NEW_MIRROR_BACKFILL_ON_START`,
`NEW_MIRROR_POLLING_ENABLED`, `NEW_MIRROR_POLL_LIMIT`, `NEW_MIRROR_POLL_SECONDS`,
`NEW_MIRROR_STARTUP_PROBE`, `NIXPACKS_START_CMD`,
`PROCESS_GROUPED_MESSAGES_IN_NEW_HANDLER`, `RELAY115_VIP1375_LAST50_ONCE`,
`ROUTE_TITLE_CHECK_INTERVAL_SECONDS`, `SERVER_URL` (MAIN's copy; the server
keeps its own), `VERIFY_DEST_TOPICS_ON_START`, `VERIFY_ROUTE_TITLES`,
`VIP7_REBUILD_FETCH_LIMIT`, `ALLOW_VIP_TOPIC_MOVE_LAST50_V1`,
`VIP7_REBUILD_FROM_RELAY508_ONCE`, `FASTVIP2_WATCHDOG_LIMIT`,
`FASTVIP2_WATCHDOG_CONCURRENCY`, `FAST_VIP_FETCH_CONCURRENCY`.

## Obsolete ALT variables (safe to delete in Railway)

Retired one-time imports and the old watchdog's page size:
`ALT_ARABIC115_LAST50_COUNT`, `ALT_ARABIC115_LAST50_FETCH_LIMIT`,
`ALT_ARABIC115_LAST50_ONCE`, `ALT_ENGINE_ROUTES`, `ALT_FORCE50_COUNT`,
`ALT_FORCE50_FETCH_LIMIT`, `ALT_FORCE50_ONCE`, `ALT_FORCE50_RUN_ID`,
`ALT_FORCE_HISTORY_ONCE`, `ALT_HISTORY_COUNT`, `ALT_HISTORY_FETCH_LIMIT`,
`ALT_HISTORY_ONCE`, `ALT_HISTORY_REQUIRE_PREMIUM`, `ALT_HISTORY_RUN_ID`,
`ALT_LAST50_COUNT`, `ALT_LAST50_FETCH_LIMIT`, `ALT_LAST50_ONCE`,
`ALT_ROUTE508_FETCH_LIMIT`, `ALT_ROUTE508_LAST50_ONCE`, `ALT_WATCHDOG_LIMIT`,
`ALT_ROUTES_JSON` (routes now come from `route_table.py`).
