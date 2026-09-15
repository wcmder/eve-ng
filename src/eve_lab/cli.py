"""Command-line entry point for local plans and server discovery."""

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import quote

import yaml

from .client import EveClient
from .backup import backup
from .restore import restore
from .initialize import initialize, prepare_panorama_console
from .bootstrap import prepare as prepare_bootstrap
from .config import load_server
from .nat import configure as configure_nat
from .securecrt import generate as generate_securecrt
from .dhcp import clear as clear_dhcp, report as report_dhcp, update_dns as update_dhcp_dns
from .deploy import apply, delete, lab_status, lifecycle, plan
from .topology import load_lab_target, load_topology


def main():
    parser = argparse.ArgumentParser(description="EVE-NG lab tooling")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Repository root")
    commands = parser.add_subparsers(dest="command", required=True)
    nat = commands.add_parser("nat", help="Manage runtime pnet1 Internet NAT through pnet0")
    nat.add_argument("nat_action", choices=["add", "remove", "status"])
    nat.add_argument("interface", choices=["pnet1"])
    nat.add_argument("--server", default="default")
    nat.add_argument("--dry-run", action="store_true", help="Inspect and show commands without changing NAT")
    dhcp = commands.add_parser("dhcp", help="Manage host DHCP leases over SSH")
    dhcp_commands = dhcp.add_subparsers(dest="dhcp_action", required=True)
    clear = dhcp_commands.add_parser("clear", help="Back up and clear pnet1 DHCP server leases")
    clear.add_argument("interface", choices=["pnet1"])
    clear.add_argument("--server", default="default")
    clear.add_argument("--dry-run", action="store_true", help="Inspect lease count and configuration without changes")
    report = dhcp_commands.add_parser("report", help="List pnet1 DHCP leases without changes")
    report.add_argument("interface", choices=["pnet1"])
    report.add_argument("--server", default="default")
    update = dhcp_commands.add_parser('update', help='Update DHCP server settings')
    update_commands = update.add_subparsers(dest='dhcp_update', required=True)
    dns = update_commands.add_parser('dns', help='Advertise DNS servers from EVE_DHCP_DNS in .env')
    dns.add_argument('interface', nargs='?', default='pnet1', choices=['pnet1'])
    dns.add_argument('--server', default='default')
    dns.add_argument('--dry-run', action='store_true', help='Preview DNS changes without modifying DHCP')
    securecrt = commands.add_parser("securecrt", help="Generate SSH sessions from the DHCP report")
    securecrt.add_argument("interface", choices=["pnet1"])
    securecrt.add_argument("--server", default="default")
    securecrt.add_argument("--output", type=Path, help="Output script (default: .state/securecrt-eve.py under root)")
    credential_options = securecrt.add_mutually_exclusive_group()
    credential_options.add_argument("--credentials", help="Saved SecureCRT credential title, e.g. eve-default")
    credential_options.add_argument("--username", help="Device SSH username (default: blank)")
    securecrt.add_argument("--interactive", action="store_true", help="Prompt for each session name")
    securecrt.add_argument("--port", type=int, default=22, help="Device SSH port (default: 22)")
    for name in ("plan", "status", "templates", "template", "apply", "start", "stop", "delete", "backup", "restore", "init", "bootstrap"):
        command = commands.add_parser(name)
        command.add_argument("--server", default="default")
        if name in ("plan", "apply", "start", "stop", "delete", "backup", "restore", "init", "bootstrap"):
            command.add_argument("lab")
        if name == "bootstrap":
            command.add_argument("--node", required=True, help="Palo Alto 11.2 node to prepare for a bootstrap test")
            command.add_argument("--check", action="store_true", help="Read node state and validate bootstrap attachment prerequisites")
            command.add_argument("--attach", action="store_true", help="Attach prepared media to the stopped node; never wipes or starts it")
        if name == "init":
            command.add_argument("--node", help="Initialize only this remote node")
            command.add_argument("--management-ip", help="Palo/Panorama management IPv4 address; requires --node (overrides lab init.yaml)")
            command.add_argument("--prepare-console", action="store_true", help="Switch one stopped Panorama or Palo Alto firewall node to Telnet serial console; does not start or initialize it")
            command.add_argument("--check", action="store_true", help="Preview config files and API console mapping without console access")
            command.add_argument("--timeout", type=int, default=600, help="Boot prompt/commit wait in seconds (default: 600)")
        if name == "restore":
            command.add_argument("--from", dest="backup_source", type=Path, required=True, help="Backup directory containing manifest.json (relative to working directory or absolute)")
            command.add_argument("--check", action="store_true", help="Validate files and preview node mapping without changing the server")
            command.add_argument("--wipe", action="store_true", help="After verified uploads, erase restored nodes' writable state to initialize from startup configs; nodes must be stopped")
        if name == "backup":
            command.add_argument("--check", action="store_true", help="Check server export support without exporting or saving files")
        if name == "delete":
            command.description = "Stop all remote nodes and permanently delete the entire remote lab. Local files are kept."
        if name == "apply":
            command.add_argument("--prune", action=argparse.BooleanOptionalAction, default=True, help="Delete undeclared nodes, networks, and stale links (default: enabled); requires stopped nodes")
        if name == "start":
            command.add_argument("--node", help="Start only this remote node by exact name")
        if name == "stop":
            command.description = "Stop every node in the remote lab, regardless of local node/link edits."
            command.add_argument("--node", help="Stop only this remote node by exact name")
            command.add_argument("--remote-folder", help="Remote folder; bypass reading topology.yaml (use / for root)")
        if name == "status":
            command.add_argument("lab", nargs="?", help="Omit for server status")
        if name == "template":
            command.add_argument("name")
    args = parser.parse_args()
    try:
        server = load_server(args.root, args.server, auth="ssh" if args.command in ("dhcp", "securecrt", "nat") else "web")
        if args.command == "nat":
            print(json.dumps(configure_nat(server, args.nat_action, args.interface, args.dry_run), indent=2))
            return
        if args.command == "securecrt":
            if not 1 <= args.port <= 65535:
                raise ValueError("SSH port must be between 1 and 65535")
            result = generate_securecrt(report_dhcp(server, args.interface),
                                       args.output or args.root / ".state/securecrt-eve.py",
                                       args.username, args.port, interactive=args.interactive, credentials=args.credentials)
            print(json.dumps(result, indent=2))
            return
        if args.command == "dhcp":
            if args.dhcp_action == 'update':
                result = update_dhcp_dns(server, args.interface, args.root, args.dry_run)
            else:
                result = (report_dhcp(server, args.interface) if args.dhcp_action == "report"
                          else clear_dhcp(server, args.interface, args.dry_run))
            print(json.dumps(result, indent=2))
            return
        if args.command in ("stop", "backup", "restore", "init", "bootstrap") or (args.command == "start" and args.node):
            topology = load_lab_target(args.root, args.lab, getattr(args, "remote_folder", None))
        else:
            topology = load_topology(args.root, args.lab) if getattr(args, "lab", None) else None
        if args.command == "plan":
            result = plan(topology, server)
        else:
            client = EveClient(server["url"], server.get("timeout", 15))
            if args.command == "init":
                client.login(server["username"], server["password"], html5=False)
            else:
                client.login(server["username"], server["password"])
            try:
                if args.command == "apply":
                    result = apply(client, topology, prune=args.prune)
                elif args.command == "init":
                    if args.prepare_console:
                        if args.management_ip:
                            raise ValueError('--prepare-console cannot be combined with --management-ip')
                        result = prepare_panorama_console(client, topology, args.node, args.check)
                    else:
                        result = initialize(client, topology, args.root, args.server, args.node, args.check, args.timeout, args.management_ip)
                elif args.command == "bootstrap":
                    result = prepare_bootstrap(client, topology, args.root, args.server, args.node, args.check, args.attach)
                elif args.command == "restore":
                    result = restore(client, topology, args.backup_source, check=args.check, wipe=args.wipe)
                elif args.command == "backup":
                    result = backup(client, topology, args.root, check=args.check)
                elif args.command == "delete":
                    result = delete(client, topology)
                elif args.command in ("start", "stop"):
                    result = lifecycle(client, topology, args.command, node_name=getattr(args, 'node', None))
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
        if args.command in ("backup", "init") and result.get("failed"):
            sys.exit(1)
    except (OSError, ValueError, RuntimeError, yaml.YAMLError) as error:
        parser.exit(1, f"Error: {error}\n")


if __name__ == "__main__":
    main()
