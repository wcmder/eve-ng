"""Invoke the dedicated pnet1 DHCP helper using configured SSH credentials."""

import json
from pathlib import Path
import shlex
import sys
from urllib.parse import urlsplit

import paramiko
from ipaddress import IPv4Address
from .device_console import environment_values


def clear(server, interface, dry_run=False):
    return _invoke(server, interface, dry_run=dry_run)


def report(server, interface):
    return _invoke(server, interface, report=True)


def update_dns(server, interface, root, dry_run=False):
    if interface != 'pnet1':
        raise ValueError('DHCP DNS updates currently support only pnet1')
    value = environment_values(root).get('EVE_DHCP_DNS', '')
    if not value.strip():
        raise ValueError('Set EVE_DHCP_DNS in .env, e.g. 8.8.8.8,1.1.1.1')
    try:
        servers = list(dict.fromkeys(str(IPv4Address(part.strip())) for part in value.split(',')))
    except ValueError:
        raise ValueError('EVE_DHCP_DNS must be a comma-separated list of IPv4 addresses') from None
    source = Path(__file__).with_name('dhcp_remote.py').read_text()
    remote = 'python3 -c ' + shlex.quote(source) + ' --update-dns ' + shlex.quote(','.join(servers))
    if dry_run:
        remote += ' --dry-run'
    return run_remote(server, remote)


def _invoke(server, interface, dry_run=False, report=False):
    if interface != "pnet1":
        raise ValueError("DHCP commands currently supports only pnet1")
    source = Path(__file__).with_name("dhcp_remote.py").read_text()
    remote = "python3 -c " + shlex.quote(source)
    if report:
        remote += " --report"
    if dry_run:
        remote += " --dry-run"
    return run_remote(server, remote)


def run_remote(server, remote):
    host = server.get("ssh_host") or urlsplit(server["url"]).hostname
    user = server["ssh_username"]
    if not host or host.startswith("-") or not user or user.startswith("-"):
        raise ValueError("Invalid SSH host or user")
    client = paramiko.SSHClient()
    try:
        client.load_system_host_keys()
        client.connect(
            hostname=host, username=user, password=server["ssh_password"],
            timeout=10, auth_timeout=10, banner_timeout=10,
            allow_agent=False, look_for_keys=False,
        )
        stdin, stdout, stderr = client.exec_command(remote, timeout=120)
        stdin.close()
        output = stdout.read()
        error = stderr.read().decode("utf-8", errors="replace").strip()
        status = stdout.channel.recv_exit_status()
        if status:
            raise RuntimeError(f"SSH remote command failed (exit {status}): {error}")
    except paramiko.AuthenticationException:
        raise RuntimeError("SSH authentication failed; check SSH credentials in .env (EVE_SSH_USERNAME/EVE_SSH_PASSWORD)") from None
    except (paramiko.SSHException, OSError) as exc:
        raise RuntimeError(f"SSH connection failed: {exc}. For a new host, verify and trust its host key using ssh {user}@{host} first.") from None
    finally:
        client.close()
    try:
        return json.loads(output)
    except ValueError:
        raise RuntimeError("Remote helper returned invalid JSON") from None


def update(server, interface, dry_run=False):
    """Prompt locally for each setting read from the dedicated remote config."""
    if interface != 'pnet1':
        raise ValueError('DHCP updates currently support only pnet1')
    source = Path(__file__).with_name('dhcp_remote.py').read_text()
    remote = 'python3 -c ' + shlex.quote(source)
    snapshot = run_remote(server, remote + ' --settings')
    changes = {}
    print('Enter keeps the current value; - removes a setting. Ctrl-C cancels.\n'
          'Values use dnsmasq syntax: ranges and DHCP options are comma-separated.', file=sys.stderr)
    try:
        for item in snapshot['settings']:
            current = item['value'] if item['value'] is not None else 'enabled'
            label = f"{item['key']} (line {item['line'] + 1}) [{current}]"
            if not item['editable']:
                print(label + ' (fixed for the dedicated pnet1 service)', file=sys.stderr)
                continue
            while True:
                print(label + ': ', end='', flush=True, file=sys.stderr)
                value = input().strip()
                if not value or value == current:
                    break
                if item['value'] is None and value not in ('-', 'enabled'):
                    print('Enter keeps this flag; - disables it.', file=sys.stderr)
                    continue
                changes[str(item['line'])] = None if value == '-' else value
                break
    except (EOFError, KeyboardInterrupt):
        raise RuntimeError('DHCP update cancelled; no settings changed') from None
    if not changes:
        return {'action': 'update', 'interface': interface, 'changed': False,
                'would_change': False, 'dry_run': dry_run, 'message': 'All settings kept unchanged'}
    payload = json.dumps({'original': snapshot['original'], 'changes': changes})
    command = remote + ' --update-settings ' + shlex.quote(payload)
    if dry_run:
        command += ' --dry-run'
    return run_remote(server, command)
