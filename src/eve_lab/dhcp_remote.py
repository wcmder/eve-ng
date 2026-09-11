"""Standalone helper executed over SSH on the EVE-NG host."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


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


if __name__ == "__main__":
    try:
        print(json.dumps(clear_leases("--dry-run" in sys.argv, report="--report" in sys.argv)))
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"DHCP error: {error}", file=sys.stderr)
        sys.exit(1)
