# Archive (4 Nov 2026 backend audit)

Nothing here is imported or run by any live Railway service. Kept for history.

- `workflows/`: GitHub Actions that rewrote `worker_fixed.py` / `routes.py` and
  pushed to `main` (a 90s connect delay; a route list that dropped every owner
  stop filter). Removed from `.github/workflows` so they can never fire.
- `scripts/`: one-off route scripts that overwrote Railway variables or the
  route table.
- `telegram_worker/`: old entrypoints and helpers with no importer from
  `worker_fixed`, `worker_clean_signal_forwarder`, `worker_signal_hub` or
  `server/main.py`.

Live start commands: MAIN `python -m telegram_worker.worker_fixed`, server
`python server/main.py`; the paused signal services keep their modules in
`telegram_worker/`.

## Iteration 4 retirement (4 Nov 2026)

One-time jobs that every production start had been re-running only to log
"already done" / "disabled" / "0 routes". Each was checked in the MAIN and ALT
startup logs before removal; their state files stay on the volumes untouched.

| Module / function | Last production state |
|---|---|
| `general_topic1_cleanup.py` | `STATE SKIP already_done=True deleted=38` |
| `vip5_retired_topic_cleanup.py` | `STATE SKIP deleted=618` (VIP5 outbound block stays in the copy path) |
| `relay7250_retired_cleanup.py` | `ALREADY CLEAN` |
| `vip1375_reseed2530.py` | `ALREADY DONE` |
| `partner_routepack_last100.py` | `ALL DONE configured=7 completed=7` |
| `relay7250_7248_to_vip10877_last20.py` | `STATE SKIP already_done=True sent=20` |
| `partner_dead_destination_guard.py` | purge `found=0 deleted=0`; no route targets -1003885074241 (contract test) |
| `force_82617_last50.py` | source in `STOPPED_SOURCE_CHATS`, never started |
| `requested_route_bootstrap.py` | 10 Hub routes `MISSING` / `ROUTE INVALID` (Hub paused) |
| `partner_3885074241_last20.py`, `wholechat_relay_3743381585_to_3216105903.py` | stub tasks that only logged `STOPPED` |
| `worker_fixed.py`: route-title checker, new-mirror probe/poller, private-live poller, blocked-sender cleanup, empty-topic last50, dest-topic guard, VIP7 rebuild, relay2→1364 v3, VIP move, Market Slayers v2 (5,175 lines) | `checked=0` / `routes=0` / `disabled` |
| `alt_worker_legacy.py`: ARABIC115 last50, FORCE50 v3, last50, guarded history, route508 last50 (2,482 lines) | env flags off; no start line logged |

Removal was mechanical: the start-up call was deleted, then a script removed
every top-level function and constant left with no reference anywhere in the
package, scripts or tests, and checked that the resulting syntax tree equals
the original minus exactly those definitions. Git history has the full code.

## Route table (4 Nov 2026)

`telegram_worker/routes.py` listed about 100 routes, of which 9 were live; the
rest were removed at import by hard stops and the owner stop policy. Every
live route of both workers now lives in `telegram_worker/route_table.py`, and
the old file is kept verbatim as `telegram_worker/routes_before_table_20261104.py`
so a paused route can be copied back into the table when its destination
returns. The derived MAIN route list was checked equal to the old one, field
by field (`tests/fixtures/main_live_routes_20261104.json`).
