"""One-shot mechanical extractor for decorated Flask route functions."""
import ast
from pathlib import Path

ROOT = Path(__file__).parent
APP = ROOT / "app.py"


def decorator_path(node):
    for dec in node.decorator_list:
        call = dec if isinstance(dec, ast.Call) else None
        fn = call.func if call else dec
        if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) and fn.value.id == "app":
            if fn.attr in {"route", "get", "post", "put", "patch", "delete"}:
                if call and call.args and isinstance(call.args[0], ast.Constant):
                    return str(call.args[0].value)
                return ""
    return None


def domain(path, name):
    p = path.lower()
    if p.startswith("/china"):
        return "china"
    if p.startswith("/inpost") or "/inpost" in p or p.endswith("/shipped") or "packing-list" in p:
        return "shipping"
    if p.startswith("/ksef") or "/ksef/" in p or p.startswith("/invoices") or "/invoice" in p or p.startswith("/api/client_invoices"):
        return "invoices"
    if p.startswith("/customers") or p.startswith("/searches") or p.startswith("/api/client/profile") or p.startswith("/api/client_stock") or p.startswith("/api/client_search"):
        return "customers"
    if p.startswith("/orders") or p.startswith("/api/client/orders") or p.startswith("/api/client_order_email") or p.startswith("/api/order"):
        return "orders"
    if p.startswith("/stock") or p.startswith("/inventory") or p.startswith("/analytics") or p.startswith("/deliver") or p.startswith("/scan") or p.startswith("/pricing") or p.startswith("/products") or p.startswith("/api/stock") or p.startswith("/api/product"):
        return "inventory"
    if p in {"/", "/login", "/logout"} or p.startswith("/settings") or p.startswith("/company") or p.startswith("/email") or p.startswith("/admin") or p.startswith("/payments") or p.startswith("/cash"):
        return "admin"
    return None


source = APP.read_text(encoding="utf-8")
lines = source.splitlines(keepends=True)
tree = ast.parse(source)
groups = {name: [] for name in ("orders", "customers", "invoices", "inventory", "shipping", "china", "admin")}
ranges = []

for node in tree.body:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        continue
    path = decorator_path(node)
    if path is None:
        continue
    target = domain(path, node.name)
    if not target:
        continue
    start = min([node.lineno] + [d.lineno for d in node.decorator_list]) - 1
    end = node.end_lineno
    # Some long parenthesised returns are reported one physical continuation
    # line short by the parser/runtime combination. Include only indented or
    # empty continuation lines; the next top-level definition is untouched.
    while end < len(lines) and (not lines[end].strip() or lines[end][:1].isspace()):
        end += 1
    groups[target].append((node.name, "".join(lines[start:end])))
    ranges.append((start, end))

routes_dir = ROOT / "routes"
routes_dir.mkdir(exist_ok=True)
(routes_dir / "__init__.py").write_text("\"\"\"Domain route registration.\"\"\"\n", encoding="utf-8")

for target, functions in groups.items():
    body = [
        '"""Mechanically extracted Flask routes; business logic is unchanged."""\n\n',
        "def register_routes(context):\n",
        "    globals().update(context)\n\n",
    ]
    for _name, chunk in functions:
        body.extend("    " + line if line.strip() else line for line in chunk.splitlines(keepends=True))
        body.append("\n")
    names = [name for name, _ in functions]
    body.append(f"    exported = {{{', '.join(repr(n) + ': ' + n for n in names)}}}\n")
    body.append("    globals().update(exported)\n")
    body.append("    return exported\n")
    (routes_dir / f"{target}.py").write_text("".join(body), encoding="utf-8")

removed_lines = {
    index
    for start, end in ranges
    for index in range(start, end)
}

marker = 'if __name__ == "__main__":'
joined = "".join(line for index, line in enumerate(lines) if index not in removed_lines)
registration = '''\n# Domain routes are registered after infrastructure and shared helpers exist.\nfrom routes import admin as routes_admin, china as routes_china, customers as routes_customers\nfrom routes import inventory as routes_inventory, invoices as routes_invoices\nfrom routes import orders as routes_orders, shipping as routes_shipping\n# Legacy code replaces the original /searches handler with client_searches_v2.\n# Temporarily release the endpoint so its URL rule can be registered in the\n# customer module, then restore the same v2 handler as before the extraction.\napp.view_functions.pop("client_searches", None)\nfor _routes_module in (routes_admin, routes_customers, routes_orders, routes_inventory, routes_shipping, routes_invoices, routes_china):\n    globals().update(_routes_module.register_routes(globals()))\nif "client_searches_v2" in globals():\n    app.view_functions["client_searches"] = client_searches_v2\n\n'''
registration += '''_DOMAIN_ROUTE_MODULES = (routes_admin, routes_customers, routes_orders, routes_inventory, routes_shipping, routes_invoices, routes_china)\n\n@app.before_request\ndef _refresh_domain_route_context():\n    context = globals()\n    for module in _DOMAIN_ROUTE_MODULES:\n        module.__dict__.update(context)\n\n'''
if marker in joined:
    joined = joined.replace(marker, registration + marker, 1)
else:
    joined += registration + '\nif __name__ == "__main__":\n    app.run(host="0.0.0.0", port=5000, debug=True)\n'
APP.write_text(joined, encoding="utf-8")

print({name: len(items) for name, items in groups.items()})
