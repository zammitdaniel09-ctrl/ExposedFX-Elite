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
        require(source != (-1004367822325, 7250), "Retired 7250 source restored")
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


def check_historical_importer():
    path = PACKAGE / "relay7250_7248_to_vip10877_last20.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    nodes = [
        target
        for node in tree.body if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and target.id == "TARGETS"
    ]
    require(len(nodes) == 1, "Expected exactly one historical importer TARGETS list")
    assignments = [
        node for node in tree.body if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "TARGETS" for t in node.targets)
    ]
    targets = assignments[0].value
    require(isinstance(targets, ast.List), "Historical target list is not literal")
    observed = []
    for item in targets.elts:
        require(isinstance(item, ast.Tuple) and len(item.elts) == 4,
                "Unexpected history target format")
        observed.append((
            ast.literal_eval(item.elts[0]),
            ast.literal_eval(item.elts[1]),
            ast.literal_eval(item.elts[3]),
        ))
    require(observed == [(-1004367822325, 7248, 10877)],
            f"Incorrect history imports configured: {observed}")
    print("PASS: retired 7250 excluded from the one-time import")


def check_worker_invariants():
    worker = (PACKAGE / "worker_fixed.py").read_text(encoding="utf-8-sig")
    for marker in (
        "[RELAY7250 PERMANENT OUTBOUND BLOCK]",
        "[VIP5 DESTINATION BLOCKED]",
        "delete_existing_vip5_messages_once(",
        "shared_strict_topic_poll(",
        "cleanup_relay7250_to_vip10877_once(",
        "remove_general_topic1_history_once(",
        "message_map = load_map()",
    ):
        require(marker in worker, f"Expected live-worker guard missing: {marker}")
    package = (PACKAGE / "__init__.py").read_text(encoding="utf-8-sig")
    require("[DOWNSTREAM ACCESS COOLDOWN]" in package,
            "Wholechat access-failure cooldown removed")
    print("PASS: worker runtime safeguards still installed")


if __name__ == "__main__":
    try:
        check_syntax()
        check_routes()
        check_historical_importer()
        check_worker_invariants()
    except (AssertionError, SyntaxError, ValueError, OSError) as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print("Forwarding safety audit complete (no credentials or Telegram requests).")
