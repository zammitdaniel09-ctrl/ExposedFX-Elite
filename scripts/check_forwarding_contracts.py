#!/usr/bin/env python3
"""Offline, standard-library-only forwarding contract and syntax checks.

Never imports Telegram worker modules, opens credentials, or contacts services.
Run in CI before changing production routes or deploying new worker code.
"""

import ast
import fnmatch
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "telegram_worker"

# Railway -> imperium-telegram-worker-alt -> Settings -> Watch Paths. ALT redeploys only
# when a matching file changes, so every module ALT imports must match one of these.
# Keep this list and Railway's identical (MAIN watches everything except tests/docs).
ALT_WATCH_PATHS = (
    "/telegram_worker/alt_*.py",
    "/telegram_worker/engine/**",
    "/telegram_worker/state_store.py",
    "/telegram_worker/route_table.py",
    "/telegram_worker/reply_hardening.py",
    "/telegram_worker/permanent_delivery_errors.py",
    "/telegram_worker/owner_forwarding_policy.py",
    "/telegram_worker/health_monitor.py",
    "/telegram_worker/liveness.py",
    "/telegram_worker/log_setup.py",
    "/telegram_worker/__init__.py",
    "/telegram_worker/runtime_guard.py",
    "/telegram_worker/community_emoji_registry.py",
    "/telegram_worker/imperium_vip_*.py",
    "/telegram_worker/topic1_kill_switch.py",
    "/requirements.txt",
    "/.python-version",
)


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
    print(f"PASS: checked {len(specs)} live routes (MAIN and ALT) and the retired-topic guard")


def module_path(name):
    path = ROOT / (name.replace(".", "/") + ".py")
    if path.exists():
        return path
    path = ROOT / name.replace(".", "/") / "__init__.py"
    return path if path.exists() else None


def imported_modules(start):
    """Every telegram_worker module `start` imports, directly or not (read with ast, never run)."""
    seen, todo = set(), [start]
    while todo:
        name = todo.pop()
        path = module_path(name)
        if name in seen or path is None:
            continue
        seen.add(name)
        package = name if path.name == "__init__.py" else name.rsplit(".", 1)[0]
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
            if isinstance(node, ast.Import):
                todo += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    parts = package.split(".")
                    base = ".".join(parts[: len(parts) - node.level + 1] + ([base] if base else []))
                todo += [base] + [f"{base}.{alias.name}" for alias in node.names]
        todo = [t for t in todo if t.startswith("telegram_worker")]
    return seen


def check_alt_watch_paths():
    modules = imported_modules("telegram_worker.alt_worker")
    require("telegram_worker.engine.intake" in modules, "ALT import scan found no engine")
    for name in sorted(modules):
        path = "/" + module_path(name).relative_to(ROOT).as_posix()
        require(
            any(fnmatch.fnmatchcase(path, pattern) for pattern in ALT_WATCH_PATHS),
            f"ALT imports {path}, which no ALT watch path covers: add it to ALT_WATCH_PATHS and to "
            "Railway (ALT service, Settings, Watch Paths), or ALT keeps running the old code",
        )
    print(f"PASS: ALT redeploys on changes to all {len(modules)} modules it imports")


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
        check_alt_watch_paths()
    except (AssertionError, SyntaxError, ValueError, OSError) as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print("Forwarding safety audit complete (no credentials or Telegram requests).")
