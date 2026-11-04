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
