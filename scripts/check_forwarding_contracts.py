#!/usr/bin/env python3
"""Offline, standard-library-only forwarding contract and syntax checks.

Never imports Telegram worker modules, opens credentials, or contacts services.
Run in CI before changing production routes or deploying new worker code.
"""

import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "telegram_worker"


def require(condition, reason):
    if not condition:
        raise AssertionError(reason)


def check_syntax():
    candidates = sorted(PACKAGE.rglob("*.py"))
    require(candidates, "No Python worker sources found")
    for path in candidates:
        source = path.read_text(encoding="utf-8-sig")
        compile(source, str(path), "exec")
    print(f"PASS: compiled {len(candidates)} Python worker files")


def load_route_table():
    """route_table.py is plain Python (no Telegram imports); loading it runs validate()."""
    spec = importlib.util.spec_from_file_location("route_table_check", PACKAGE / "route_table.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


def check_routes():
    table = load_route_table()
    specs = table.TABLE
    relay, vip = table.RELAY, table.VIP
    require({s.worker for s in specs} == {"MAIN", "ALT"}, "Route table must list both workers")
    require(
        sum(s.identity == (relay, 7248, vip, 10877) for s in specs) == 1,
        "Exactly one 7248 -> VIP10877 route required",
    )
    owner = {s.identity for s in specs if s.path == "owner"}
    for contract in (
        (relay, 7250, vip, 12161),
        (vip, 1364, table.MARKET_MAKERS, 28),
        (vip, 10877, table.MARKET_MAKERS, 8),
        (vip, 12161, table.MARKET_MAKERS, 24),
    ):
        require(contract in owner, f"Owner route missing: {contract}")

    # The table's own guard must refuse every retired path.
    for source, dest in (
        ((relay, 2530), (vip, 9)),
        ((relay, 2), (relay, 2530)),
        ((relay, 7250), (vip, 11)),
        ((relay, 2), (vip, 5)),
        ((relay, 2), (-1003885074241, 4663)),
        ((relay, 2), (-1003885074241, 4665)),
    ):
        bad = table.RouteSpec("retired", "MAIN", "intake", source[0], source[1], dest[0], dest[1])
        try:
            table.validate((bad,))
        except ValueError:
            continue
        raise AssertionError(f"Route table accepted a retired path: {source} -> {dest}")

    routes_py = (PACKAGE / "routes.py").read_text(encoding="utf-8-sig")
    require(
        'routes("MAIN", "intake", "owner")' in routes_py, "routes.py must take MAIN routes from the table"
    )
    print(f"PASS: checked {len(specs)} live routes (MAIN and ALT), retired-topic blocks and VIP10877")


def check_worker_invariants():
    worker = (PACKAGE / "worker_fixed.py").read_text(encoding="utf-8-sig")
    for marker in ("message_map = open_message_map(", "liveness.exit_disconnected("):
        require(marker in worker, f"Expected live-worker guard missing: {marker}")
    policy = (PACKAGE / "owner_forwarding_policy.py").read_text(encoding="utf-8-sig")
    for paused in ("-1003216105903", "-1003918958200", "-1004442967052"):
        require(paused in policy, f"Paused destination no longer stopped: {paused}")
    engine = (PACKAGE / "engine" / "rules.py").read_text(encoding="utf-8-sig")
    for marker in (
        'return "vip5_retired"',
        'return "relay7250_retired"',
        "RELAY_7250_ONLY_DESTINATION = (-1003726286301, 12161)",
    ):
        require(marker in engine, f"Engine retired-path stop missing: {marker}")
    deliver = (PACKAGE / "engine" / "deliver.py").read_text(encoding="utf-8-sig")
    for marker in ("[RELAY7250 PERMANENT OUTBOUND BLOCK]", "[VIP5 DESTINATION BLOCKED]"):
        require(marker in deliver, f"Engine retired-path log marker missing: {marker}")
    print("PASS: worker runtime safeguards still installed")


if __name__ == "__main__":
    try:
        check_syntax()
        check_routes()
        check_worker_invariants()
    except (AssertionError, SyntaxError, ValueError, OSError) as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print("Forwarding safety audit complete (no credentials or Telegram requests).")
