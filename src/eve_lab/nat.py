"""Run the pnet1 NAT helper over SSH."""
from pathlib import Path
import shlex
from .dhcp import run_remote


def configure(server, action, interface, dry_run=False):
    if interface != 'pnet1' or action not in ('add', 'remove', 'status'):
        raise ValueError('NAT supports add/remove/status for pnet1 only')
    source = Path(__file__).with_name('nat_remote.py').read_text()
    command = 'python3 -c ' + shlex.quote(source) + ' ' + action
    if dry_run:
        command += ' --dry-run'
    return run_remote(server, command)
