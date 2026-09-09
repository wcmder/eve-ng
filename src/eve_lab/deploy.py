"""Additive EVE-NG deployment and lab lifecycle operations."""

from urllib.parse import quote

from .client import EveAPIError
from .topology import interface_key, validate


def lab_path(topology):
    folder = topology.get("remote_folder", "/").rstrip("/")
    return "labs" + quote(f"{folder}/{topology['name']}.unl", safe="/")


def plan(topology: dict, server: dict) -> dict:
    return {
        "lab": topology["name"], "server": server["url"],
        "remote_folder": topology.get("remote_folder", "/"),
        **{key: len(topology[key]) for key in ("nodes", "networks", "links")},
        "note": "Local validation and summary only; no remote comparison or changes.",
    }


def indexed(data):
    if isinstance(data, dict):
        return {str(key): value for key, value in data.items()}
    if isinstance(data, list):
        return {str(value.get("id", index)): value for index, value in enumerate(data)}
    raise RuntimeError("Expected an EVE-NG object collection")


def named(client, path):
    result = {}
    for ident, item in indexed(client.request("GET", path)).items():
        name = item["name"]
        if name in result:
            raise RuntimeError(f"Duplicate remote name {name} in {path}; resolve before applying")
        result[name] = {**item, "id": ident}
    return result


def check_settings(desired, actual):
    for key, value in desired.items():
        if key in ("left", "top"):
            continue  # Preserve manual canvas layout.
        if str(actual.get(key)) != str(value):
            raise RuntimeError(
                f"Conflict on {desired['name']}.{key}: remote={actual.get(key)!r}, "
                f"requested={value!r}. Existing objects are not overwritten."
            )


def interfaces(client, path, node):
    data = client.request("GET", f"{path}/nodes/{node['id']}/interfaces")
    return indexed(data["ethernet"])


def resolve(ports, name):
    matches = [(ident, port) for ident, port in ports.items()
               if interface_key(port["name"]) == interface_key(name)]
    if len(matches) != 1:
        available = ", ".join(port["name"] for port in ports.values())
        raise RuntimeError(f"Cannot uniquely resolve interface {name}; available: {available}")
    return matches[0]


def check_link(client, path, link, nodes, networks):
    node = nodes[link["node"]]
    ident, port = resolve(interfaces(client, path, node), link["interface"])
    target = networks.get(link["network"], {}).get("id")
    current = str(port.get("network_id", 0))
    if current != "0" and current != target:
        raise RuntimeError(f"Conflict: {link['node']} {link['interface']} already connects to network {current}")
    if current != target and str(node.get("status")) != "0":
        raise RuntimeError(f"Stop {link['node']} before adding interface connections")
    return ident, current


def apply(client, topology):
    validate(topology)
    path = lab_path(topology)
    # Preflight templates/images and network types before creating anything.
    payloads = {}
    for node in topology["nodes"]:
        template = client.request("GET", "list/templates/" + quote(node["template"], safe=""))
        if template.get("type") != node["type"]:
            raise ValueError(f"Template type does not match {node['name']}")
        options = template["options"]
        if node["image"] not in options.get("image", {}).get("list", {}):
            raise ValueError(f"Image {node['image']} is not available for {node['template']}")
        defaults = {key: option["value"] for key, option in options.items()
                    if "value" in option and key != "uuid"}
        payloads[node["name"]] = {
            **defaults, "uuid": "", "left": 200, "top": 200,
            **node, "numberNodes": 1,
        }
    types = client.request("GET", "list/networks")
    for network in topology["networks"]:
        if network["type"] not in types:
            raise ValueError(f"Unavailable network type: {network['type']}")
    folder = topology.get("remote_folder", "/").rstrip("/")
    client.request("GET", "folders" + quote(folder, safe="/") + "/")
    try:
        client.request("GET", path)
        exists = True
    except EveAPIError as error:
        if error.code != 404:
            raise
        exists = False
    nodes = named(client, path + "/nodes") if exists else {}
    networks = named(client, path + "/networks") if exists else {}
    for kind, existing in (("nodes", nodes), ("networks", networks)):
        for desired in topology[kind]:
            if desired["name"] in existing:
                check_settings(desired, existing[desired["name"]])
    for link in topology["links"]:
        if link["node"] in nodes:
            check_link(client, path, link, nodes, networks)
    changes = []
    try:
        if not exists:
            client.request("POST", "labs", {
                "path": folder or "/", "name": topology["name"], "version": "1",
                "author": "eve", "description": topology.get("description", ""), "body": "",
            })
            changes.append("created lab")
        for kind, existing in (("networks", networks), ("nodes", nodes)):
            for desired in topology[kind]:
                name = desired["name"]
                if name not in existing:
                    payload = payloads[name] if kind == "nodes" else desired
                    client.request("POST", path + "/" + kind, payload)
                    changes.append(f"created {kind}: {name}")
                    existing.update(named(client, path + "/" + kind))
                    if name not in existing:
                        raise RuntimeError(f"Created {name} was not returned by server")
                    check_settings(desired, existing[name])
        # Resolve all links before writing any connections. New-node interface
        # names can only be checked after EVE-NG has created the node.
        pending = []
        for link in topology["links"]:
            ident, current = check_link(client, path, link, nodes, networks)
            target = networks[link["network"]]["id"]
            if current != target:
                pending.append((link, ident, target))
        for link, ident, target in pending:
            node = nodes[link["node"]]
            endpoint = f"{path}/nodes/{node['id']}/interfaces"
            client.request("PUT", endpoint, {ident: target})
            changes.append(f"connected {link['node']} {link['interface']} to {link['network']}")
            _, port = resolve(interfaces(client, path, node), link["interface"])
            if str(port.get("network_id")) != target:
                raise RuntimeError("Server did not persist the requested connection")
    except (RuntimeError, ValueError) as error:
        raise RuntimeError(
            f"Apply did not complete: {error}. Completed: {changes}. "
            "No rollback performed; inspect the lab and rerun after resolving the error."
        ) from error
    return {"lab": topology["name"], "path": path, "changes": changes,
            "message": "Applied; no nodes started" if changes else "Already matches; no changes"}


def lifecycle(client, topology, action):
    if action not in ("start", "stop"):
        raise ValueError(f"Unsupported action: {action}")
    path = lab_path(topology)
    nodes = named(client, path + "/nodes")
    missing = [node["name"] for node in topology["nodes"] if node["name"] not in nodes]
    if missing:
        raise RuntimeError(f"Missing nodes: {missing}; run eve apply first")
    completed = []
    try:
        for desired in topology["nodes"]:
            node = nodes[desired["name"]]
            if str(node.get("status")) == ("2" if action == "start" else "0"):
                continue
            client.request("GET", f"{path}/nodes/{node['id']}/{action}")
            completed.append(desired["name"])
    except RuntimeError as error:
        raise RuntimeError(f"{action} failed: {error}; completed nodes: {completed}") from error
    return {"lab": topology["name"], "action": action, "changed_nodes": completed}


def lab_status(client, topology):
    path = lab_path(topology)
    return {"lab": topology["name"], "nodes": client.request("GET", path + "/nodes"),
            "networks": client.request("GET", path + "/networks")}


def delete(client, topology):
    """Delete the whole remote lab, including nodes not declared locally."""
    path = lab_path(topology)
    result = {"lab": topology["name"], "path": path, "deleted": False, "stopped_nodes": []}
    try:
        client.request("GET", path)
    except EveAPIError as error:
        if error.code != 404:
            raise
        return {**result, "message": "Lab already absent; no changes"}
    try:
        nodes = indexed(client.request("GET", path + "/nodes"))
        for ident, node in nodes.items():
            if str(node.get("status")) != "0":
                client.request("GET", f"{path}/nodes/{ident}/stop")
                result["stopped_nodes"].append(node["name"])
        remaining = indexed(client.request("GET", path + "/nodes"))
        if any(str(node.get("status")) != "0" for node in remaining.values()):
            raise RuntimeError("Some nodes are still active; lab was not deleted")
        client.request("DELETE", path)
        try:
            client.request("GET", path)
        except EveAPIError as error:
            if error.code != 404:
                raise
        else:
            raise RuntimeError("Server still reports the lab after deletion")
    except RuntimeError as error:
        raise RuntimeError(
            f"Delete did not complete: {error}. Stopped nodes: {result['stopped_nodes']}. "
            "Inspect remote state before retrying."
        ) from error
    return {**result, "deleted": True, "message": "Remote lab deleted; local files retained"}
