"""Command-line entry point for local plans and server discovery."""

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import quote

import yaml

from .client import EveClient
from .config import load_server
from .dhcp import clear as clear_dhcp
from .deploy import apply, delete, lab_status, lifecycle, plan
from .topology import load_lab_target, load_topology


def main():
    parser = argparse.ArgumentParser(description="EVE-NG lab tooling")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Repository root")
    commands = parser.add_subparsers(dest="command", required=True)
    dhcp = commands.add_parser("dhcp", help="Manage host DHCP leases over SSH")
    dhcp_commands = dhcp.add_subparsers(dest="dhcp_action", required=True)
    clear = dhcp_commands.add_parser("clear", help="Back up and clear pnet1 DHCP server leases")
    clear.add_argument("interface", choices=["pnet1"])
    clear.add_argument("--server", default="default")
    clear.add_argument("--dry-run", action="store_true", help="Inspect lease count and configuration without changes")
    for name in ("plan", "status", "templates", "template", "apply", "start", "stop", "delete"):
        command = commands.add_parser(name)
        command.add_argument("--server", default="default")
        if name in ("plan", "apply", "start", "stop", "delete"):
            command.add_argument("lab")
        if name == "delete":
            command.description = "Stop all remote nodes and permanently delete the entire remote lab. Local files are kept."
        if name == "apply":
            command.add_argument("--prune", action="store_true", help="Delete remote nodes/networks absent from YAML; requires stopped nodes")
        if name == "stop":
            command.description = "Stop every node in the remote lab, regardless of local node/link edits."
            command.add_argument("--remote-folder", help="Remote folder; bypass reading topology.yaml (use / for root)")
        if name == "status":
            command.add_argument("lab", nargs="?", help="Omit for server status")
        if name == "template":
            command.add_argument("name")
    args = parser.parse_args()
    try:
        server = load_server(args.root, args.server, auth="ssh" if args.command == "dhcp" else "web")
        if args.command == "dhcp":
            print(json.dumps(clear_dhcp(server, args.interface, args.dry_run), indent=2))
            return
        if args.command == "stop":
            topology = load_lab_target(args.root, args.lab, args.remote_folder)
        else:
            topology = load_topology(args.root, args.lab) if getattr(args, "lab", None) else None
        if args.command == "plan":
            result = plan(topology, server)
        else:
            client = EveClient(server["url"], server.get("timeout", 15))
            client.login(server["username"], server["password"])
            try:
                if args.command == "apply":
                    result = apply(client, topology, prune=args.prune)
                elif args.command == "delete":
                    result = delete(client, topology)
                elif args.command in ("start", "stop"):
                    result = lifecycle(client, topology, args.command)
                elif args.command == "status" and topology:
                    result = lab_status(client, topology)
                else:
                    path = {
                        "status": "status",
                        "templates": "list/templates/",
                        "template": "list/templates/" + quote(getattr(args, "name", ""), safe=""),
                    }[args.command]
                    result = client.request("GET", path)
            finally:
                try:
                    client.logout()
                except RuntimeError as error:
                    print(f"Logout warning: {error}", file=sys.stderr)
        print(json.dumps(result, indent=2))
    except (OSError, ValueError, RuntimeError, yaml.YAMLError) as error:
        parser.exit(1, f"Error: {error}\n")


if __name__ == "__main__":
    main()
