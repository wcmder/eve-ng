"""Additive EVE-NG deployment and lab lifecycle operations."""

from urllib.parse import quote
import time

from .client import EveAPIError
from .topology import expand_links, interface_key, validate

STOP_TIMEOUT = 30


def wait_for_stopped(client, path):
    """EVE-NG may acknowledge stop before the VM process has exited."""
    deadline = time.monotonic() + STOP_TIMEOUT
    while True:
        nodes = indexed(client.request("GET", path + "/nodes"))
        if all(str(node.get("status")) == "0" for node in nodes.values()):
            return nodes
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return nodes
        time.sleep(min(1, remaining))


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


def check_direct_bridges(client, path, direct, nodes, networks):
    """Never hide a shared LAN when converting a visible bridge to a cable."""
    for name, attachments in direct:
        if name not in networks:
            continue
        expected = set()
        for link in attachments:
            if link["node"] in nodes:
                node = nodes[link["node"]]
                ident, _ = resolve(interfaces(client, path, node), link["interface"])
                expected.add((node["id"], ident))
        actual = set()
        for node in nodes.values():
            for ident, port in interfaces(client, path, node).items():
                if str(port.get("network_id")) == networks[name]["id"]:
                    actual.add((node["id"], ident))
        if actual - expected:
            raise RuntimeError(f"Direct link {name} has other attached interfaces; cannot hide a shared network")


def prune_objects(client, path, topology, changes):
    """Remove undeclared objects only after the desired topology was applied."""
    nodes = named(client, path + "/nodes")
    networks = named(client, path + "/networks")
    keep_nodes = {node["name"] for node in topology["nodes"]}
    keep_networks = {network["name"] for network in topology["networks"]}
    if any(str(node.get("status")) != "0" for node in nodes.values()):
        raise RuntimeError("Stop all nodes in the lab before pruning")
    for name, node in nodes.items():
        if name not in keep_nodes:
            client.request("DELETE", f"{path}/nodes/{node['id']}")
            changes.append(f"pruned node: {name}")
            if name in named(client, path + "/nodes"):
                raise RuntimeError(f"Server did not delete node {name}")
    nodes = named(client, path + "/nodes")
    # Node deletion may also remove now-unused hidden networks.
    networks = named(client, path + "/networks")
    for name, network in networks.items():
        if name in keep_networks:
            continue
        attached = []
        for node in nodes.values():
            for ident, port in interfaces(client, path, node).items():
                if str(port.get("network_id")) == network["id"]:
                    attached.append((node, ident, port["name"]))
        # EVE-NG deleteNetwork() unlinks attached interfaces before saving.
        # PUT to network 0 is invalid; interface DELETE is version-dependent.
        if name in named(client, path + "/networks"):
            client.request("DELETE", f"{path}/networks/{network['id']}")
        changes.append(f"pruned network: {name}")
        if name in named(client, path + "/networks"):
            raise RuntimeError(f"Server did not delete network {name}")
        for node, ident, port_name in attached:
            if str(interfaces(client, path, node)[ident].get("network_id")) != "0":
                raise RuntimeError(f"Server did not disconnect {node['name']} {port_name}")
            changes.append(f"disconnected {node['name']} {port_name} from {name}")


def apply(client, topology, prune=False):
    validate(topology)
    topology, direct = expand_links(topology)
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
    if prune and any(str(node.get("status")) != "0" for node in nodes.values()):
        raise RuntimeError("Stop all nodes in the lab before pruning: eve stop <lab>")
    check_direct_bridges(client, path, direct, nodes, networks)
    updates = []
    for kind, existing in (("nodes", nodes), ("networks", networks)):
        for desired in topology[kind]:
            if desired["name"] in existing:
                actual = existing[desired["name"]]
                resources = {key: desired[key] for key in ("cpu", "ram")
                             if kind == "nodes" and key in desired
                             and str(desired[key]) != str(actual.get(key))}
                check_settings({key: value for key, value in desired.items() if key not in resources}, actual)
                if resources:
                    if str(actual.get("status")) != "0":
                        raise RuntimeError(f"Stop {desired['name']} before changing CPU or RAM")
                    updates.append((desired, actual["id"], resources))
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
        for desired, ident, resources in updates:
            # Check again immediately before the write in case the node was started.
            current = named(client, path + "/nodes")[desired["name"]]
            if current["id"] != ident or str(current.get("status")) != "0":
                raise RuntimeError(f"Node {desired['name']} changed or started during apply; retry after stopping it")
            # EVE-NG's edit() changes CPU/RAM without setting its modified flag.
            # Sending the unchanged name triggers persistence without a rename.
            client.request("PUT", f"{path}/nodes/{ident}", {"name": current["name"], **resources})
            changes.append(f"updated {desired['name']}: {resources}")
            nodes = named(client, path + "/nodes")
            check_settings(desired, nodes[desired["name"]])
        for kind, existing in (("networks", networks), ("nodes", nodes)):
            for desired in topology[kind]:
                name = desired["name"]
                if name not in existing:
                    # Hidden bridges with no links are discarded on save by EVE-NG.
                    payload = payloads[name] if kind == "nodes" else {"visibility": 1, **desired}
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
        check_direct_bridges(client, path, direct, nodes, networks)
        for name, _ in direct:
            network = networks[name]
            if str(network.get("visibility", 1)) != "0":
                client.request("PUT", f"{path}/networks/{network['id']}", {"visibility": 0})
                changes.append(f"hid direct-link bridge: {name}")
                updated = named(client, path + "/networks")
                if name not in updated or str(updated[name].get("visibility")) != "0":
                    raise RuntimeError(f"Server did not hide direct-link bridge {name}")
        if prune:
            prune_objects(client, path, topology, changes)
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
    if action == "stop":
        return stop_all(client, topology)
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


def stop_all(client, topology):
    path = lab_path(topology)
    nodes = indexed(client.request("GET", path + "/nodes"))
    requested, completed, failures = {}, [], []
    for ident, node in nodes.items():
        if str(node.get("status")) == "0":
            continue
        try:
            client.request("GET", f"{path}/nodes/{ident}/stop")
            requested[ident] = node["name"]
        except RuntimeError as error:
            failures.append(f"{node['name']} (ID {ident}): {error}")
    try:
        remaining = wait_for_stopped(client, path)
        completed = [name for ident, name in requested.items()
                     if ident in remaining and str(remaining[ident].get("status")) == "0"]
        active = [f"{node['name']} (ID {ident})" for ident, node in remaining.items()
                  if str(node.get("status")) != "0"]
        if active:
            failures.append(f"Nodes still active after {STOP_TIMEOUT}s: {active}")
    except RuntimeError as error:
        failures.append(f"Could not verify stopped state: {error}")
    if failures:
        raise RuntimeError(f"Stop incomplete: {'; '.join(failures)}. Completed nodes: {completed}. Stop requests accepted: {list(requested.values())}")
    return {"lab": topology["name"], "action": "stop", "changed_nodes": completed}


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
        remaining = wait_for_stopped(client, path)
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
