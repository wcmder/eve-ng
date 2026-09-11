"""Standalone IPv4 NAT helper; owns only EVE_PNET1_NAT and its jump."""
import ipaddress
import json
import os
import shlex
import subprocess
import sys

CHAIN = 'EVE_PNET1_NAT'
TAG = 'eve-pnet1-internet'
# Recognize the previous managed rule set for migration/removal.
LEGACY_EXCLUDED = ('0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8',
            '169.254.0.0/16', '172.16.0.0/12', '192.0.0.0/24', '192.0.2.0/24',
            '192.168.0.0/16', '198.18.0.0/15', '198.51.100.0/24',
            '203.0.113.0/24', '224.0.0.0/4', '240.0.0.0/4')


def run(*args):
    result = subprocess.run(args, text=True, capture_output=True, timeout=20)
    if result.returncode:
        raise RuntimeError('{}: {}'.format(shlex.join(args), result.stderr.strip()))
    return result.stdout.strip()


def rule(*args):
    return ['-A', CHAIN, *args[:-2], '-m', 'comment', '--comment', TAG, *args[-2:]]


def configure(action, dry_run=False):
    if os.geteuid() != 0:
        raise RuntimeError('NAT requires SSH as root')
    if action not in ('add', 'remove', 'status'):
        raise ValueError('Expected add, remove, or status')
    expected = [rule('!', '-d', '172.16.0.0/24', '-j', 'MASQUERADE')]
    legacy = [rule('-d', network, '-j', 'RETURN') for network in LEGACY_EXCLUDED]
    legacy.append(rule('-j', 'MASQUERADE'))
    snapshot = [shlex.split(line) for line in run('iptables', '-w', '5', '-t', 'nat', '-S').splitlines()]
    exists = ['-N', CHAIN] in snapshot
    contents = [r for r in snapshot if r[:2] == ['-A', CHAIN]]
    jumps = [r for r in snapshot if '-j' in r and r[r.index('-j') + 1] == CHAIN]
    if action == 'status':
        valid_jump = (len(jumps) == 1 and len(jumps[0]) == 12 and
                      jumps[0][:3] == ['-A', 'POSTROUTING', '-s'] and
                      jumps[0][4:] == ['-o', 'pnet0', '-m', 'comment', '--comment', TAG, '-j', CHAIN])
        state = ('absent' if not exists and not jumps else
                 'configured' if exists and valid_jump and contents == expected else
                 'legacy' if exists and valid_jump and contents == legacy else 'unexpected')
        return {'action': action, 'interface': 'pnet1', 'out_interface': 'pnet0',
                'managed_state': state, 'changed': False,
                'ipv4_forwarding': run('sysctl', '-n', 'net.ipv4.ip_forward') == '1',
                'nat_rules': [shlex.join(r) for r in snapshot],
                'managed_rules': [shlex.join(r) for r in contents],
                'managed_jumps': [shlex.join(r) for r in jumps]}
    if contents and contents not in (expected, legacy):
        raise RuntimeError('Managed NAT chain has unexpected rules; refusing to overwrite it')
    for jump in jumps:
        # Only the exact owned POSTROUTING jump may be removed.
        if (len(jump) != 12 or jump[:3] != ['-A', 'POSTROUTING', '-s'] or
                jump[4:] != ['-o', 'pnet0', '-m', 'comment', '--comment', TAG, '-j', CHAIN]):
            raise RuntimeError('Unexpected reference to managed NAT chain; inspect manually')
    commands = []
    result = {'action': action, 'interface': 'pnet1', 'out_interface': 'pnet0',
              'dry_run': dry_run, 'persistent': False}
    if action == 'add':
        addresses = json.loads(run('ip', '-j', '-4', 'address', 'show', 'dev', 'pnet1'))
        ips = [a for dev in addresses for a in dev['addr_info'] if a.get('scope') == 'global']
        if len(ips) != 1:
            raise RuntimeError('Expected exactly one global IPv4 address on pnet1')
        subnet = str(ipaddress.ip_interface('{}/{}'.format(ips[0]['local'], ips[0]['prefixlen'])).network)
        route = json.loads(run('ip', '-j', '-4', 'route', 'get', '1.1.1.1'))
        if not route or route[0].get('dev') != 'pnet0':
            raise RuntimeError('Internet route must use pnet0')
        if run('sysctl', '-n', 'net.ipv4.ip_forward') != '1':
            raise RuntimeError('Enable IPv4 forwarding on the host before adding NAT')
        if run('iptables', '-w', '5', '-S', 'FORWARD') != '-P FORWARD ACCEPT':
            raise RuntimeError('Custom forwarding firewall detected; review forwarding rules before adding NAT')
        desired = ['-A', 'POSTROUTING', '-s', subnet, '-o', 'pnet0', '-m', 'comment', '--comment', TAG, '-j', CHAIN]
        if jumps and jumps != [desired]:
            raise RuntimeError('Existing NAT source differs or has duplicate jumps; remove NAT before adding again')
        if not exists:
            commands.append(['-N', CHAIN])
        if contents == legacy:
            commands.append(['-F', CHAIN])
        if contents != expected:
            commands.extend(expected)
        if not jumps:
            commands.append(desired)
        result.update(subnet=subnet, gateway=ips[0]['local'], excluded_destinations=['172.16.0.0/24'])
    else:
        commands.extend([['-D', *jump[1:]] for jump in jumps])
        if exists:
            commands.extend([['-F', CHAIN], ['-X', CHAIN]])
    result['commands'] = [shlex.join(['iptables', '-w', '5', '-t', 'nat', *cmd]) for cmd in commands]
    result['changed'] = False
    if dry_run:
        return result
    completed = []
    try:
        for command in commands:
            run('iptables', '-w', '5', '-t', 'nat', *command)
            completed.append(shlex.join(command))
    except RuntimeError as error:
        raise RuntimeError('{}; completed: {}. No rollback; inspect NAT rules before retrying.'.format(error, completed)) from error
    result['changed'] = bool(completed)
    result['message'] = ('NAT removed for new connections; existing conntrack mappings may remain until expiry'
                         if action == 'remove' else 'Runtime NAT configured; rerun after host reboot')
    return result


if __name__ == '__main__':
    try:
        print(json.dumps(configure(sys.argv[1], '--dry-run' in sys.argv)))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print('NAT error: {}'.format(error), file=sys.stderr)
        sys.exit(1)
