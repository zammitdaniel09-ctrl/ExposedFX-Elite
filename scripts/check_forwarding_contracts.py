#!/usr/bin/env python3
"""Offline, standard-library-only forwarding contract and syntax checks.

Never imports Telegram worker modules, opens credentials, or contacts services.
Run in CI before changing production routes or deploying new worker code.
"""
import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "telegram_worker"


def require(condition, reason):
    if not condition:
        raise AssertionError(reason)


def literal_route_dicts(source):
    tree = ast.parse(source, filename=str(PACKAGE / "routes.py"))
    result = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        try:
            route = ast.literal_eval(node)
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
            continue
        if not isinstance(route, dict):
            continue
        if {"source_chat", "source_topic", "dest_chat", "dest_topic"} <= route.keys():
            result.append(route)
    return result


def check_syntax():
    candidates = sorted(PACKAGE.rglob("*.py"))
    require(candidates, "No Python worker sources found")
    for path in candidates:
        source = path.read_text(encoding="utf-8-sig")
        compile(source, str(path), "exec")
    print(f"PASS: compiled {len(candidates)} Python worker files")


def check_routes():
    content = (PACKAGE / "routes.py").read_text(encoding="utf-8-sig")
    routes = literal_route_dicts(content)
    require(len(routes) >= 50, "Suspiciously few literal forwarding routes")

    # Retired topics 2530 and 7250 are not permitted outbound.
    for route in routes:
        source = (int(route["source_chat"]), int(route["source_topic"] or 0))
        dest = (int(route["dest_chat"]), int(route["dest_topic"] or 0))
        require(source != (-1004367822325, 2530), "Retired 2530 source restored")
        require(dest != (-1004367822325, 2530), "Retired 2530 destination restored")
        require(source != (-1004367822325, 7250) or dest == (-1003726286301, 12161),
                "7250 is authorized only to VIP12161")
        require(dest != (-1003726286301, 5), "Retired VIP5 destination restored")
        require(dest != (-1003885074241, 4663), "Retired partner 4663 restored")
        require(dest != (-1003885074241, 4665), "Retired partner 4665 restored")

    wanted = [
        route for route in routes
        if (
            int(route["source_chat"]), int(route["source_topic"] or 0),
            int(route["dest_chat"]), int(route["dest_topic"] or 0)
        ) == (-1004367822325, 7248, -1003726286301, 10877)
    ]
    require(len(wanted) == 1, "Exactly one 7248 -> VIP10877 route required")
    new_contracts = {(-1004367822325, 7250, -1003726286301, 12161),
                     (-1003726286301, 1364, -1003786932333, 28),
                     (-1003726286301, 10877, -1003786932333, 8),
                     (-1003726286301, 12161, -1003786932333, 24)}
    for contract in new_contracts:
        require(sum(tuple(r[k] for k in ("source_chat","source_topic","dest_chat","dest_topic")) == contract for r in routes) == 1,
                f"Owner route missing/duplicate: {contract}")

    identities = [
        (
            int(route["source_chat"]), int(route["source_topic"] or 0),
            int(route["dest_chat"]), int(route["dest_topic"] or 0)
        )
        for route in routes
    ]
    require(len(set(identities)) == len(identities),
            "Duplicate source-topic/destination-topic route declared")

    for marker in (
        "# END RELAY2530 FORCE STOP V1",
        "# END RELAY7250 PERMANENT OUTBOUND HARD STOP V1",
        "# END VIP5 DESTINATION RETIRED V1",
        "# END PARTNER DEAD DESTINATION HARD STOP V1",
    ):
        require(marker in content, f"Required route hard stop missing: {marker}")
    print(f"PASS: checked {len(routes)} routes, retired-topic blocks and VIP10877")


def check_worker_invariants():
    worker = (PACKAGE / "worker_fixed.py").read_text(encoding="utf-8-sig")
    for marker in (
        "message_map = load_map()",
        "liveness.exit_disconnected(",
    ):
        require(marker in worker, f"Expected live-worker guard missing: {marker}")
    policy = (PACKAGE / "owner_forwarding_policy.py").read_text(encoding="utf-8-sig")
    for paused in ("-1003216105903", "-1003918958200", "-1004442967052"):
        require(paused in policy, f"Paused destination no longer stopped: {paused}")
    engine = (PACKAGE / "engine" / "rules.py").read_text(encoding="utf-8-sig")
    for marker in ('return "vip5_retired"', 'return "relay7250_retired"', "RELAY_7250_ONLY_DESTINATION = (-1003726286301, 12161)"):
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
