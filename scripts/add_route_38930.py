import ast
import pprint
from pathlib import Path

routes_path = Path("telegram_worker/routes.py")
text = routes_path.read_text(encoding="utf-8-sig")
tree = ast.parse(text)
routes = None
for node in tree.body:
    if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "ROUTES" for t in node.targets):
        routes = ast.literal_eval(node.value)
        break
if routes is None:
    raise SystemExit("Could not read ROUTES")

route = {
    "name": "Mirror Route38930 2817163788 16218",
    "source_chat": -1002817163788,
    "source_topic": 16218,
    "dest_chat": -1003918958200,
    "dest_topic": 38930,
    "verify_title": False,
}

exists = any(
    int(r.get("source_chat")) == route["source_chat"]
    and int(r.get("source_topic")) == route["source_topic"]
    and int(r.get("dest_chat")) == route["dest_chat"]
    and int(r.get("dest_topic")) == route["dest_topic"]
    for r in routes
)
if not exists:
    routes.append(route)

routes_path.write_text(
    "import os\n\n"
    "# telegram_worker/routes.py\n"
    "# source_topic=None means all messages from that source.\n\n"
    "ROUTES = " + pprint.pformat(routes, width=120, sort_dicts=False) + "\n\n"
    "if os.environ.get(\"DISABLE_PROVIDER_ROUTES\", \"0\").strip() == \"1\":\n"
    "    ROUTES = []\n",
    encoding="utf-8",
)

hub_path = Path("telegram_worker/worker_signal_hub.py")
hub = hub_path.read_text(encoding="utf-8-sig")
line = next(x for x in hub.splitlines() if x.startswith("DEFAULT_ALLOWED_TOPICS ="))
if "38930" not in line:
    hub = hub.replace('29327,29452,26902"', '29327,29452,26902,38930"', 1)
    hub_path.write_text(hub, encoding="utf-8")

print("ROUTE_38930_READY")
