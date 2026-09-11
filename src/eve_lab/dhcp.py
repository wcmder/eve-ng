"""Invoke the dedicated pnet1 DHCP helper using configured SSH credentials."""

import json
from pathlib import Path
import shlex
from urllib.parse import urlsplit

import paramiko


def clear(server, interface, dry_run=False):
    return _invoke(server, interface, dry_run=dry_run)


def report(server, interface):
    return _invoke(server, interface, report=True)


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
