import json
import shlex
import unittest
from unittest.mock import patch
from eve_lab import nat_remote as nat


class NatTests(unittest.TestCase):
    def setUp(self):
        self.rules = [['-P', 'POSTROUTING', 'ACCEPT'], ['-A', 'POSTROUTING', '-s', '172.16.0.0/24', '-j', 'MASQUERADE']]
        self.original = [r[:] for r in self.rules]
        self.calls = []
        self.forwarding = '1'
        self.route = 'pnet0'
        self.addCleanup(patch.stopall)
        patch.object(nat.os, 'geteuid', return_value=0).start()
        patch.object(nat, 'run', side_effect=self.run_command).start()

    def run_command(self, *args):
        self.calls.append(args)
        if args[0] == 'sysctl': return self.forwarding
        if args[:4] == ('ip', '-j', '-4', 'route'): return json.dumps([{'dev': self.route}])
        if args[0] == 'ip': return json.dumps([{'addr_info': [{'local': '172.16.1.1', 'prefixlen': 24, 'scope': 'global'}]}])
        if args[-2:] == ('-S', 'FORWARD'): return '-P FORWARD ACCEPT'
        cmd = list(args[5:])
        if cmd == ['-S']: return '\n'.join(shlex.join(r) for r in self.rules)
        if cmd[0] in ('-N', '-A'): self.rules.append(cmd)
        elif cmd[0] == '-D': self.rules.remove(['-A', *cmd[1:]])
        elif cmd[0] == '-F': self.rules = [r for r in self.rules if r[:2] != ['-A', nat.CHAIN]]
        elif cmd[0] == '-X': self.rules.remove(['-N', nat.CHAIN])
        else: raise AssertionError(args)
        return ''

    def test_add_repeat_remove_preserves_unrelated_rules(self):
        self.assertTrue(nat.configure('add')['changed'])
        self.assertFalse(nat.configure('add')['changed'])
        self.assertTrue(nat.configure('remove')['changed'])
        self.assertEqual(self.rules, self.original)
        self.assertFalse(nat.configure('remove')['changed'])

    def test_dry_run_and_preflight_no_mutation(self):
        self.assertTrue(nat.configure('add', True)['commands'])
        self.assertEqual(self.rules, self.original)
        self.route = 'eth0'
        with self.assertRaisesRegex(RuntimeError, 'pnet0'): nat.configure('add')
        self.assertEqual(self.rules, self.original)
        self.route = 'pnet0'
        self.forwarding = '0'
        with self.assertRaisesRegex(RuntimeError, 'forwarding'): nat.configure('add')

    def test_unexpected_chain_refused(self):
        self.rules += [['-N', nat.CHAIN], ['-A', nat.CHAIN, '-j', 'ACCEPT']]
        with self.assertRaisesRegex(RuntimeError, 'unexpected rules'): nat.configure('remove')

    def test_wireguard_exempt_rule_and_legacy_migration(self):
        legacy = [nat.rule('-d', network, '-j', 'RETURN') for network in nat.LEGACY_EXCLUDED]
        legacy.append(nat.rule('-j', 'MASQUERADE'))
        self.rules += [['-N', nat.CHAIN], *legacy]
        self.assertTrue(nat.configure('add')['changed'])
        rules = [r for r in self.rules if r[:2] == ['-A', nat.CHAIN]]
        self.assertEqual(rules, [nat.rule('!', '-d', '172.16.0.0/24', '-j', 'MASQUERADE')])
        self.assertFalse(nat.configure('add')['changed'])
        nat.configure('remove')
        self.assertEqual(self.rules, self.original)

    def test_status_is_read_only_and_reports_unexpected_rules(self):
        self.assertEqual(nat.configure('status')['managed_state'], 'absent')
        nat.configure('add')
        original = [r[:] for r in self.rules]
        status = nat.configure('status')
        self.assertEqual(status['managed_state'], 'configured')
        self.assertTrue(status['ipv4_forwarding'])
        self.assertEqual(len(status['nat_rules']), len(self.rules))
        self.assertEqual(self.rules, original)
        self.rules.append(['-A', nat.CHAIN, '-j', 'ACCEPT'])
        status = nat.configure('status')
        self.assertEqual(status['managed_state'], 'unexpected')
        self.assertFalse(status['changed'])
