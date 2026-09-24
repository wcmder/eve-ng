"""Standalone helper executed over SSH on the EVE-NG host."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import tempfile
from ipaddress import IPv4Address


def run(*args):
    result = subprocess.run(args, text=True, capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError(f"{' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def clear_leases(dry_run=False, config_path="/etc/eve-dhcp/pnet1.conf",
                 lease_path="/var/lib/eve-dhcp/pnet1.leases", report=False):
    interface = "pnet1"
    unit = "eve-pnet1-dhcp.service"
    config = Path(config_path)
    leases = Path(lease_path)
    if os.geteuid() != 0:
        raise RuntimeError("DHCP lease access requires SSH as root")
    options = {}
    for line in config.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        options.setdefault(key.strip(), []).append(value.strip())
    if options.get("interface") != [interface] or options.get("dhcp-leasefile") != [str(leases)]:
        raise RuntimeError("DHCP configuration is not dedicated to pnet1 and its expected lease file")
    if {"conf-file", "conf-dir", "dhcp-script"} & options.keys():
        raise RuntimeError("Additional DHCP configuration/scripts require manual review")
    command = run("systemctl", "show", unit, "--property=ExecStart", "--value")
    if "dnsmasq" not in command or f"--conf-file={config}" not in command:
        raise RuntimeError("DHCP service does not use the expected pnet1 configuration")
    if run("systemctl", "is-active", unit) != "active":
        raise RuntimeError("pnet1 DHCP service is not active")
    if leases.is_symlink() or not leases.is_file():
        raise RuntimeError("Expected a regular pnet1 lease file")
    if report:
        records = []
        now = time.time()
        for number, line in enumerate(leases.read_text().splitlines(), 1):
            if not line.strip():
                continue
            fields = line.split()
            if len(fields) != 5:
                raise RuntimeError(f"Invalid DHCP lease record on line {number}")
            expiry, mac, address, hostname, client_id = fields
            try:
                expiry = int(expiry)
                if expiry < 0:
                    raise ValueError()
                expires_at = datetime.fromtimestamp(expiry, timezone.utc).isoformat() if expiry else None
            except (ValueError, OverflowError, OSError):
                raise RuntimeError(f"Invalid DHCP lease expiry on line {number}") from None
            records.append({"ip_address": address, "mac_address": mac,
                            "hostname": None if hostname == "*" else hostname,
                            "client_id": None if client_id == "*" else client_id,
                            "expires_at": expires_at,
                            "remaining_seconds": max(0, int(expiry - now)) if expiry else None,
                            "status": "permanent" if not expiry else "active" if expiry > now else "expired"})
        return {"interface": interface, "service": unit, "lease_count": len(records), "leases": records}
    result = {"interface": interface, "service": unit, "dry_run": dry_run,
              "lease_count": len(leases.read_text().splitlines())}
    if dry_run:
        return result
    run("systemctl", "stop", unit)
    backup = leases.with_name(leases.name + f".backup-{time.time_ns()}")
    try:
        # Copy after stopping so dnsmasq cannot overwrite the cleared database.
        shutil.copy2(leases, backup)
        result["backup"] = str(backup)
        result["lease_count"] = len(leases.read_text().splitlines())
        leases.write_text("")
    except Exception as error:
        raise RuntimeError(f"Lease cleanup failed; backup path: {backup}: {error}") from error
    finally:
        try:
            run("systemctl", "start", unit)
        except RuntimeError as error:
            raise RuntimeError(f"DHCP restart failed; lease backup path: {backup}: {error}") from error
    if run("systemctl", "is-active", unit) != "active":
        raise RuntimeError(f"DHCP service did not restart; lease backup: {backup}")
    result["leases_after_restart"] = len(leases.read_text().splitlines())
    result["message"] = "Server lease records cleared; clients may renew immediately"
    return result


def update_dns(servers, dry_run=False, config_path='/etc/eve-dhcp/pnet1.conf',
               lease_path='/var/lib/eve-dhcp/pnet1.leases'):
    servers = list(dict.fromkeys(str(IPv4Address(value.strip())) for value in servers))
    if not servers:
        raise ValueError('At least one DNS IPv4 address is required')
    # Reuse the dedicated-service checks; report mode does not modify leases.
    clear_leases(config_path=config_path, lease_path=lease_path, report=True)
    config = Path(config_path)
    if config.is_symlink() or not config.is_file():
        raise RuntimeError('Expected a regular DHCP configuration file')
    original = config.read_text()
    lines = []
    previous = []
    for line in original.splitlines():
        key, sep, value = line.strip().partition('=')
        if sep and key in ('dhcp-option', 'dhcp-option-force'):
            parts = [p.strip() for p in value.split(',')]
            if any(p in ('6', 'option:dns-server') for p in parts):
                if parts[0] not in ('6', 'option:dns-server'):
                    raise RuntimeError('Tagged DHCP DNS options require manual configuration')
                previous.append(line.strip())
                continue
        lines.append(line)
    option = 'dhcp-option=option:dns-server,' + ','.join(servers)
    updated = '\n'.join(lines + [option]) + '\n'
    result = {'action': 'update-dns', 'interface': 'pnet1', 'dns_servers': servers,
              'previous_options': previous, 'dry_run': dry_run, 'changed': False,
              'would_change': updated != original, 'backup': None}
    if dry_run or updated == original:
        return result
    return save_settings(config, original, updated, result)


def save_settings(config, original, updated, result):
    fd, temporary = tempfile.mkstemp(prefix='.pnet1-dns-', dir=config.parent)
    backup = config.with_name(config.name + '.backup-' + str(time.time_ns()))
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(updated)
        run('dnsmasq', '--test', '--conf-file=' + temporary)
        if config.read_text() != original:
            raise RuntimeError('DHCP configuration changed during validation; retry')
        shutil.copy2(config, backup)
        stat = config.stat()
        os.chmod(temporary, stat.st_mode & 0o777)
        os.chown(temporary, stat.st_uid, stat.st_gid)
        os.replace(temporary, config)
        try:
            run('systemctl', 'restart', 'eve-pnet1-dhcp.service')
            if run('systemctl', 'is-active', 'eve-pnet1-dhcp.service') != 'active':
                raise RuntimeError('DHCP service did not become active')
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            shutil.copy2(backup, config)
            try:
                run('systemctl', 'restart', 'eve-pnet1-dhcp.service')
                if run('systemctl', 'is-active', 'eve-pnet1-dhcp.service') != 'active':
                    raise RuntimeError('DHCP service is inactive')
            except (RuntimeError, OSError, subprocess.SubprocessError):
                raise RuntimeError(f'DHCP update failed and DHCP recovery failed; inspect service; backup: {backup}') from None
            raise RuntimeError(f'DHCP update failed; previous configuration restored; backup: {backup}') from error
        result.update(changed=True, backup=str(backup), service='active',
                      message='DHCP settings updated; clients receive new settings on renewal. Leases were not cleared.')
        return result
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


# These identify the dedicated service, rather than client DHCP settings.
FIXED_SETTINGS = {'interface', 'dhcp-leasefile'}


def settings_snapshot(config_path='/etc/eve-dhcp/pnet1.conf',
                      lease_path='/var/lib/eve-dhcp/pnet1.leases'):
    clear_leases(config_path=config_path, lease_path=lease_path, report=True)
    config = Path(config_path)
    if config.is_symlink() or not config.is_file():
        raise RuntimeError('Expected a regular DHCP configuration file')
    original = config.read_text()
    settings = []
    for index, line in enumerate(original.splitlines()):
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        key, separator, value = stripped.partition('=')
        key = key.strip()
        settings.append({'line': index, 'key': key,
                         'value': value.strip() if separator else None,
                         'editable': key not in FIXED_SETTINGS})
    return {'interface': 'pnet1', 'original': original, 'settings': settings}


def update_settings(payload, dry_run=False, config_path='/etc/eve-dhcp/pnet1.conf',
                    lease_path='/var/lib/eve-dhcp/pnet1.leases'):
    snapshot = settings_snapshot(config_path, lease_path)
    original = snapshot['original']
    if payload['original'] != original:
        raise RuntimeError('DHCP configuration changed while prompting; rerun the command')
    lines = original.splitlines(keepends=True)
    editable = {str(item['line']): item for item in snapshot['settings'] if item['editable']}
    for index, value in payload['changes'].items():
        if index not in editable:
            raise ValueError('Cannot change the dedicated interface or lease file')
        item = editable[index]
        if value is None:
            lines[int(index)] = ''
        else:
            if not isinstance(value, str) or any(c in value for c in ('\n', '\r', '\x00')):
                raise ValueError('DHCP settings must be single-line values')
            if item['value'] is None:
                if value != 'enabled':
                    raise ValueError('Flag settings must be enabled or removed')
                replacement = item['key']
            else:
                replacement = item['key'] + '=' + value
            lines[int(index)] = replacement + '\n'
    updated = ''.join(lines)
    result = {'action': 'update', 'interface': 'pnet1', 'dry_run': dry_run,
              'changed': False, 'would_change': updated != original, 'backup': None,
              'configuration': updated}
    if dry_run or updated == original:
        return result
    return save_settings(Path(config_path), original, updated, result)


if __name__ == "__main__":
    try:
        if '--settings' in sys.argv:
            result = settings_snapshot()
        elif '--update-settings' in sys.argv:
            payload = json.loads(sys.argv[sys.argv.index('--update-settings') + 1])
            result = update_settings(payload, dry_run='--dry-run' in sys.argv)
        elif '--update-dns' in sys.argv:
            servers = sys.argv[sys.argv.index('--update-dns') + 1].split(',')
            result = update_dns(servers, dry_run='--dry-run' in sys.argv)
        else:
            result = clear_leases("--dry-run" in sys.argv, report="--report" in sys.argv)
        print(json.dumps(result))
    except (OSError, RuntimeError, ValueError, IndexError, subprocess.SubprocessError) as error:
        print(f"DHCP error: {error}", file=sys.stderr)
        sys.exit(1)
