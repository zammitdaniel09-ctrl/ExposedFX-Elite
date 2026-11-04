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

- MAIN: everything except `telegram_worker/alt_*.py`, `tests/test_alt_*.py`
  and this file.
- ALT: `telegram_worker/alt_*.py`, `owner_forwarding_policy.py`,
  `health_monitor.py`, `liveness.py`, `log_setup.py`, `__init__.py`,
  `runtime_guard.py`, `requirements.txt` (the only modules ALT imports). Add a
  module here if ALT starts importing it.
- server: `server/**`, `requirements.txt`.

The old `alt-worker` branch is frozen as a rollback point (Railway → ALT →
Settings → Source → branch `alt-worker`).

## Flow

```
providers ──ALT──▶ VIP RELAY GROUP (-1004367822325) ──MAIN──▶ live destinations
                         topics 2, 115, 7248, 7250, 8323
SamGtrades (-1004281603170) ──MAIN (whole chat, house format)──▶ Exposed | VIP
Market Slayers VIP (-1003838973021) ──MAIN──▶ IMPERIUM FX VIP topic 9
```

### ALT routes (5)
| Route | Source | Relay topic |
|---|---|---|
| ALT RELAY 1 | Private Circle G Trades | 2 |
| ARABIC VIP EN -> RELAY115 | T4T AE Scalping VIP (translated to English) | 115 |
| NewAccount Topic5246 To Relay7248 | Blackline / 5246 | 7248 B+SCALPING |
| NewAccount Topic5247 To Relay7250 | Blackline / 5247 | 7250 B+XAU |
| NewAccount Select7194 To Relay8323 | SELECT SIGNALS / 7194 | 8323 select trade ideas |

### MAIN routes
| Path | Source | Destination | Format |
|---|---|---|---|
| FASTVIP2 router | Market Slayers VIP | IMPERIUM FX VIP · ᴍᴀʀᴋᴇᴛ ꜱʟᴀʏᴇʀꜱ ᴠɪᴘ (9) | copy |
| FASTVIP2 router | relay 106 AKX | VIP 11 | copy |
| FASTVIP2 router | relay 2 | VIP Extra Partner 2 (1364) | copy |
| FASTVIP2 router | relay 508 XAU Exclusive Room | VIP 7 | Imperium VIP format |
| FASTVIP2 router | relay 7248 | VIP EXTRA PARTNER 1 (10877) | copy |
| owner routes (`owner_requested_routes_20261102.py`) | relay 7250 | VIP EXTRA PARTNER [ 4 ] (12161) | copy |
| owner routes | VIP 1364 / 10877 / 12161 | ᴍᴀʀᴋᴇᴛ ᴍᴀᴋᴇʀꜱ 28 / 8 / 24 | copy |
| `owner_formatted_route_20261103.py` | relay 8323 | VIP EXTRA PARTNER [ 5 ] (12289) | library format (signals, updates, recaps) |
| `wholechat_relay_4281603170_to_3743381585.py` | Signals \| SamGtrades | Exposed \| VIP | house library format |

Formatters: `library_signal_format.py` (EXTRA PARTNER [ 5 ]),
`samgtrades_house_formatter.py` (Exposed | VIP), `imperium_vip_*` (VIP 7).
Custom emoji ids come from the owner's Saved Messages (`community_emoji_registry.py`).

## Delivery engine (`telegram_worker/engine/`)

The clean replacement for the legacy copy pipeline: typed routes and plans,
pure delivery rules, reply-target resolution, formatter plugins and a planner
(see the package docstring). It runs in **shadow mode** on MAIN: every live
post on the FASTVIP2 and owner routes is planned by the engine and compared
with what the legacy pipeline actually sent. Shadow mode never sends; it
logs `[SHADOW MATCH_SEND]` / `[SHADOW MATCH_SKIP]` / `[SHADOW MISMATCH]` and
keeps totals in `engine_shadow_v1.json`. Set `ENGINE_SHADOW=0` on MAIN to
turn it off. A route moves to the engine only after its shadow totals show
no mismatch on live traffic.

**Moving a route to the engine:** set `ENGINE_ROUTES` on MAIN to a comma
separated list of route names (e.g. `VIP RELAY Topic2 To VIP1364`). Those
routes are then delivered by `engine/deliver.py` (same Telegram calls, same
map, same log markers with `engine=True lag=…`); all others stay on legacy
under shadow comparison. Remove the name to roll back; nothing else changes.

Replay audit (4 Nov 2026, `engine/audit.py`): 156/156 recent posts on the 7
active routes planned identically to the copies in the destinations. Relay
topics 106 and 508 have no feeding ALT route (dormant), so they had nothing
to compare.

The rules are tested against the legacy functions themselves
(`tests/test_engine.py`): content filter, sender block, VIP hard block, loop
guard, reply targets and the VIP topic 7 formatter on generated corpora.

## Safety layers

1. `owner_forwarding_policy.py` — `STOPPED_DEST_CHATS` / `STOPPED_SOURCE_CHATS`
   remove routes at startup; `install_send_guard` refuses any send, forward or
   edit into a stopped chat on every MAIN code path.
2. Runtime stops (`owner_stopped_forwarding_20261102.json` on the volume) are
   written only for real access loss; "entity/topic unavailable" must repeat
   3 times over 10 minutes first.
3. Paused routes are archived in `docs/paused_routes_20261103.md` (45 routes).
4. CI: `forwarding-safety.yml` compiles both workers, checks route contracts
   and runs every unit test (MAIN and ALT) on each push.

## Monitoring

Logs are one JSON object per line on Railway (`log_setup.py`), so the log
explorer shows real severities (`warn` for notices, `error` only for errors)
and `@logger:imperium-worker` style filters work.

`liveness.py` (both workers) restarts a worker that has stopped working
without crashing: if Telegram drops the connection for good, or the event loop
stalls for 120 s, the process exits non-zero so Railway starts a fresh one,
and the next start sends "♻️ … restarted itself" with the reason to that
account's Saved Messages. A redeploy no longer waits for the old run's lock:
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

A digest of deliveries, failures and lag per route arrives daily at 08:00 Malta.

## State on the volumes (never delete or overwrite)

MAIN `/app/data`: session (rewritten from `SESSION_B64_*` at start),
`message_map.json` (source→destination ids for replies/edits; an unreadable
file is copied to `message_map.corrupt-*.json`), owner route/formatter state,
wholechat map, stop ledger, `health_monitor_v1.json`, `engine_shadow_v1.json`,
`liveness_exit_v1.json` (only between a self-restart and the next start).
ALT `/data-alt`: session, message map, route checkpoints, last-N import state,
`health_monitor_v1.json`, `liveness_exit_v1.json`.

## Runbook

- **Restore a paused destination:** remove its id from `STOPPED_DEST_CHATS`
  in `owner_forwarding_policy.py`, update `tests/test_live_route_contract.py`,
  push to `main`. Routes resume from their saved checkpoints.
- **Resume a route stopped at runtime:** delete its entry from
  `owner_stopped_forwarding_20261102.json` on the volume, then restart.
- **Roll back a deploy:** Railway → service → Deployments → previous → Redeploy.
  State files are forward-compatible; nothing needs undoing.
- **New Telegram session:** generate it with an interactive login on a trusted
  machine, store it only in Railway variables, never in chat or git.
- **Before any change:** `python scripts/check_forwarding_contracts.py` and
  `python -m unittest discover -s tests` must pass.
