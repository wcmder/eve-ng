"""Load a lab definition independently of its target server."""

from pathlib import Path
import re

import yaml


def interface_key(name: str) -> str:
    return re.sub(r"^(gigabitethernet|gi|g)(?=\d)", "gi", name.lower().replace(" ", ""))


def validate(topology: dict):
    folder = topology.get("remote_folder", "/")
    if not isinstance(folder, str) or not folder.startswith("/") or any(
        part in (".", "..") for part in folder.split("/")
    ):
        raise ValueError("remote_folder must be an absolute EVE-NG folder without . or ..")
    allowed = {
        "nodes": {"name", "template", "type", "image", "cpu", "ram", "ethernet", "console", "left", "top"},
        "networks": {"name", "type", "left", "top"},
        "links": {"node", "interface", "network"},
    }
    for kind, fields in allowed.items():
        if not isinstance(topology.get(kind), list):
            raise ValueError(f"Topology {kind} must be a list")
        for item in topology[kind]:
            if not isinstance(item, dict) or set(item) - fields:
                raise ValueError(f"Invalid or unsupported fields in {kind}: {item}")
    names = {}
    for kind, required in (("nodes", ("name", "template", "type", "image")), ("networks", ("name", "type"))):
        names[kind] = set()
        for item in topology[kind]:
            for field in required:
                if not isinstance(item.get(field), str) or not item[field].strip():
                    raise ValueError(f"{kind}: {field} must be a nonempty string")
            if item["name"] in names[kind]:
                raise ValueError(f"Duplicate {kind} name: {item['name']}")
            names[kind].add(item["name"])
    for node in topology["nodes"]:
        if node["type"] != "qemu":
            raise ValueError("Deployment currently supports QEMU nodes only")
        if "/" in node["image"] or "\\" in node["image"]:
            raise ValueError("image must be a directory basename, not a path")
        for field in ("cpu", "ram", "ethernet"):
            if field in node and (type(node[field]) is not int or node[field] < 1):
                raise ValueError(f"{node['name']}: {field} must be a positive integer")
    endpoints = set()
    for link in topology["links"]:
        if any(not isinstance(link.get(field), str) or not link[field] for field in allowed["links"]):
            raise ValueError("Each link needs node, interface, and network strings")
        if link["node"] not in names["nodes"] or link["network"] not in names["networks"]:
            raise ValueError(f"Unknown node or network in link: {link}")
        endpoint = (link["node"], interface_key(link["interface"]))
        if endpoint in endpoints:
            raise ValueError(f"Interface connected more than once: {endpoint}")
        endpoints.add(endpoint)


def load_topology(root: Path, lab: str) -> dict:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", lab):
        raise ValueError("Lab names may contain letters, digits, underscores, and hyphens")
    topology = yaml.safe_load((root / "labs" / lab / "topology.yaml").read_text())
    if not isinstance(topology, dict) or topology.get("name") != lab:
        raise ValueError("Topology name must match its lab directory")
    validate(topology)
    return topology
