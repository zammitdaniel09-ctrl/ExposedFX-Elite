# Forwarding reliability — operational notes (26 October 2026)

## Service ownership

- MAIN: `imperium-telegram-worker` on GitHub `main`, start `python -m telegram_worker.worker_fixed`.
- ALT: `imperium-telegram-worker-alt` on `alt-worker`, start `python -m telegram_worker.alt_worker`.
- Clean signal forwarder: `exposedfx-clean-signal-forwarder` on `main`, start `python -m telegram_worker.worker_clean_signal_forwarder`.
- Signal formatter: `exposedfx-ai-signal-formatter` on `main`.
- Other Railway services can redeploy on pushes to `main` even if their entrypoint did not change.

## Hard routing contracts

- **NEVER** forward outbound from `-1004367822325/7250`; this source was retired by the owner. ALT's earlier incoming `5247 -> 7250` route is separate.
- **NEVER** send into or read from `-1004367822325/2530`.
- Preserve `-1004367822325/7248 -> -1003726286301/10877` with its existing mapping and last-20 import state.
- Preserve retired partner destinations `-1003885074241/4663` and `/4665`.
- General `-1003726286301/1` cleanup is an explicitly requested one-time deletion; it is NOT a ban on later General messages.
- Never reset `DATA_DIR`, `message_map.json`, session files, the ALT route checkpoints, or import state during performance work.

## Performance policy

- Fast healthy routes keep their existing cadence. Strict source/topic polling may share a response for at most 0.4s when the same strict topic is requested simultaneously.
- A missing Telegram access hash or `ChannelPrivateError` on a legacy whole-chat sidecar is not fixed by retrying in a 10-second loop. Back off to 300s (minimum 60s); automatically retry after the cooldown.
- Other downstream exceptions retain the existing 10s retry; preserve logs needed for diagnosis.
- ALT legacy PAIR106 access failures use their own 300s cooldown.
- Never bypass album grouping, live-only baselines, source-topic strictness, reply mapping, deduplication, or filtered-content guards for speed.

## Safe verification

1. Run `python scripts/check_forwarding_contracts.py` with no credentials. It compiles the worker source files and checks critical route contracts.
2. Confirm GitHub checks before merging.
3. Confirm Railway deployments are SUCCESS; this **does not** imply live Telegram forwarding is functional.
4. Inspect recent runtime logs without filtering away the exception's final line.
5. Confirm live copies with a genuine new message; compare source and destination mapping. Do not replay history to test.
6. If Clean Signal Forwarder reports CRASHED, capture its full, unfiltered exception. Do **not** modify its credentials or restart it repeatedly based on speculation.

## Open operational issues

- Previous runtime report showed `exposedfx-clean-signal-forwarder` CRASHED. The exact exception is unavailable in that report.
- MAIN showed repeated `wholechat_relay_3743381585_to_3216105903.py` entity-resolution tracebacks. The cooldown reduces retry load for known permission/entity errors, but it does not repair revoked Telegram access.
- Test coverage is static; a passing audit is not a live Telegram integration test.
