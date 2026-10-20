import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from eve_lab.initialize import config_commands, initialize
from eve_lab.versa_console import VersaConsole, VersaHeadendConsole, versa_credentials


class VersaConsoleTests(unittest.TestCase):
    def console(self, replies, headend=False):
        console = (VersaHeadendConsole if headend else VersaConsole)(MagicMock())
        console.send = MagicMock()
        replies = iter(replies)

        def expect(pattern, timeout=60, wake=False):
            output = next(replies)
            match = re.search(pattern, output, re.M)
            self.assertIsNotNone(match, (pattern, output))
            return output, match

        console.expect = expect
        return console

    def test_vos_shell_login_config_and_password_commit(self):
        password = 'New !"quote\\secret'
        console = self.console([
            'versa login:', 'Password:', '[admin@versa: ~] $',
            'cli\nadmin@versa-cli>',
            'configure\n[edit]\nadmin@versa-cli(config)%',
            'set interfaces vni-0/0 description WAN\n[edit]\nadmin@versa-cli(config)%',
            '[edit]\nadmin@versa-cli(config)%',
            'commit\nCommit complete.\n[edit]\nadmin@versa-cli(config)%',
            'exit\nadmin@versa-cli>',
        ])
        console.login('admin', password, 'old')
        console.initialize(['set interfaces vni-0/0 description WAN'], 'admin', password)
        sent = [call.args[0] for call in console.send.call_args_list]
        self.assertEqual(sent[:4], ['', 'admin', 'old', 'cli'])
        self.assertIn('set system users admin password ' + json.dumps(password), sent)
        self.assertFalse(any('login shell' in command or 'role admin' in command for command in sent))
        self.assertEqual(sent[-2:], ['commit', 'exit'])

    def test_operational_login_and_forced_password_change(self):
        console = self.console([
            'login:', 'Password:', '(current) UNIX password:', 'New password:',
            'Retype new password:', 'passwd: password updated successfully\nadmin@vos-cli>',
        ])
        console.login('admin', 'new', 'old')
        self.assertTrue(console.password_changed)
        self.assertEqual(console.mode, 'cli')
        self.assertEqual([c.args[0] for c in console.send.call_args_list],
                         ['', 'admin', 'old', 'old', 'new', 'new'])

    def test_login_failures_wizard_and_wrong_user(self):
        for replies in (
            ['login:', 'Password:', 'Login incorrect'],
            ['login:', 'Password:', 'Password:'],
            ['Do you want to set up network interface configuration?'],
            ['other@vos:~$'],
            ['admin@vos-cli(config)%'],
        ):
            with self.subTest(replies=replies):
                console = self.console(replies)
                with self.assertRaises(RuntimeError) as error:
                    console.login('admin', 'private-target', 'private-current')
                self.assertNotIn('private-target', str(error.exception))
                self.assertNotIn('private-current', str(error.exception))

    def test_headend_declines_optional_setup(self):
        for prompt in ('Do you want to enter setup?', 'Do you want to enter setup? (yes/no):',
                       'Do you want to enter setup? [y/N]'):
            with self.subTest(prompt=prompt):
                console = self.console(['login:', 'Password:', prompt, 'Administrator@director:~$'],
                                       headend=True)
                console.login('Administrator', 'new', 'old')
                self.assertEqual([c.args[0] for c in console.send.call_args_list],
                                 ['', 'Administrator', 'old', 'no'])
                self.assertEqual(console.mode, 'shell')

    def test_optional_setup_is_bounded_and_other_wizards_stop(self):
        for prompts in (
            ['Do you want to enter setup?', 'Do you want to enter setup?'],
            ['Do you want to set up network interface configuration?'],
        ):
            console = self.console(prompts, headend=True)
            with self.assertRaises(RuntimeError):
                console.login('Administrator', 'new', 'old')
            self.assertLessEqual([c.args[0] for c in console.send.call_args_list].count('no'), 1)

    def test_headend_passwd_never_sent_as_shell_command(self):
        console = self.console([
            'director login:', 'Password:', 'Administrator@director:~$',
            '(current) UNIX password:', 'Enter new UNIX password:',
            'Retype new UNIX password:',
            'passwd: password updated successfully\nAdministrator@director:~$',
        ], headend=True)
        # passwd also uses the "Enter new UNIX password" variant.
        console.login('Administrator', 'desired', 'current')
        console.initialize([], 'Administrator', 'desired')
        self.assertEqual([c.args[0] for c in console.send.call_args_list],
                         ['', 'Administrator', 'current', 'passwd', 'current', 'desired', 'desired'])

    def test_stale_login_failure_before_credentials_is_discarded_once(self):
        console = self.console(['Login incorrect', 'vos login:', 'Password:', 'admin@vos-cli>'])
        console.login('admin', 'desired', 'existing')
        self.assertEqual([c.args[0] for c in console.send.call_args_list],
                         ['', 'admin', 'existing'])
        console = self.console(['Login incorrect', 'Login incorrect'])
        with self.assertRaisesRegex(RuntimeError, 'login rejected'):
            console.login('admin', 'desired', 'existing')
        self.assertEqual([c.args[0] for c in console.send.call_args_list], [''])

    def test_current_credentials_rejection_is_not_retried(self):
        console = self.console(['login:', 'Password:', 'Login incorrect'])
        with self.assertRaisesRegex(RuntimeError, r'\*_CURRENT_PASSWORD') as error:
            console.login('admin', 'desired-secret', 'existing-secret')
        self.assertNotIn('existing-secret', str(error.exception))
        self.assertEqual([c.args[0] for c in console.send.call_args_list],
                         ['', 'admin', 'existing-secret'])

    def test_first_login_password_policy_error_is_distinct(self):
        console = self.console(['login:', 'Password:', 'New password:', 'BAD PASSWORD: too short'])
        with self.assertRaisesRegex(RuntimeError, 'first-login password change'):
            console.login('admin', 'desired', 'existing')

    def test_headend_unchanged_password_skips_passwd_but_runs_cfg(self):
        console = self.console([
            'versa@analytics:~$', 'cli\nAdministrator@analytics>',
            'configure\nAdministrator@analytics%',
            'set system ntp server ntp.example.test prefer iburst\nAdministrator@analytics%',
            'commit\nCommit complete.\nAdministrator@analytics%',
            'exit\nAdministrator@analytics>',
        ], headend=True)
        console.login('versa', 'same', 'same')
        console.initialize(['set system ntp server ntp.example.test prefer iburst'], 'versa', 'same')
        sent = [c.args[0] for c in console.send.call_args_list]
        self.assertNotIn('passwd', sent)
        self.assertFalse(any('system users' in value for value in sent))

    def test_failed_password_change_is_not_success(self):
        console = self.console([
            'versa@analytics:~$', 'New password:', 'Retype new password:',
            'passwd: Authentication token manipulation error\nversa@analytics:~$',
        ], headend=True)
        console.login('versa', 'new', 'old')
        with self.assertRaisesRegex(RuntimeError, 'password change failed'):
            console.initialize([], 'versa', 'new')

    def test_failed_config_or_unconfirmed_commit_not_success(self):
        for result in ('syntax error: invalid value', 'Aborted: validation failed',
                       'Commit queued', 'Error: commit failed\nCommit complete.'):
            with self.subTest(result=result):
                console = self.console([
                    'admin@vos-cli>', 'configure\nadmin@vos-cli(config)%',
                    'admin@vos-cli(config)%',
                    result + '\nadmin@vos-cli(config)%',
                ])
                console.login('admin', 'new', 'old')
                with self.assertRaises(RuntimeError):
                    console.initialize([], 'admin', 'new')

    def test_rejected_file_command_stops_before_password_and_commit(self):
        console = self.console([
            'admin@vos-cli>', 'configure\nadmin@vos-cli(config)%',
            'syntax error: unknown element\nadmin@vos-cli(config)%',
        ])
        console.login('admin', 'new', 'old')
        with self.assertRaisesRegex(RuntimeError, 'rejected a command'):
            console.initialize(['set invalid-element value', 'set system ntp server ntp.example'], 'admin', 'new')
        sent = [c.args[0] for c in console.send.call_args_list]
        self.assertEqual(sent, ['', 'configure', 'set invalid-element value'])


class VersaInitTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.base = self.root / 'labs/test/configs'
        self.base.mkdir(parents=True)
        self.client = MagicMock()

    def node(self, template):
        (self.base / 'node-init.cfg').write_text('# Credentials-only init\n')
        self.client.request.return_value = {'1': {
            'name': 'node', 'template': template, 'status': 2,
            'console': 'telnet', 'url': 'telnet://example.test:32769'}}

    @patch('eve_lab.initialize.paramiko.SSHClient')
    def test_check_all_versa_templates_without_ssh(self, ssh):
        for template in ('versafvnf', 'versavnf', 'versadir', 'versaana'):
            self.node(template)
            result = initialize(self.client, {'name': 'test'}, self.root, 'default', check=True)
            self.assertEqual(result['planned'][0]['template'], template)
            self.assertEqual(result['planned'][0]['port'], 32769)
        ssh.assert_not_called()

    @patch.dict('os.environ', {}, clear=True)
    def test_credentials_and_current_password_environment_override(self):
        (self.root / '.env').write_text('VERSA_USERNAME=admin\nVERSA_PASSWORD=desired\n'
                                       'VERSA_CURRENT_PASSWORD=existing\n'
                                       'VERSA_DIRECTOR_USERNAME=Administrator\nVERSA_DIRECTOR_PASSWORD=director\n'
                                       'VERSA_ANALYTICS_USERNAME=versa\nVERSA_ANALYTICS_PASSWORD=analytics\n')
        self.assertEqual(versa_credentials(self.root, 'versafvnf'), ('admin', 'desired', 'existing'))
        self.assertEqual(versa_credentials(self.root, 'versadir'), ('Administrator', 'director', 'director'))
        self.assertEqual(versa_credentials(self.root, 'versaana'), ('versa', 'analytics', 'analytics'))
        with patch.dict('os.environ', {'VERSA_CURRENT_PASSWORD': '', 'VERSA_PASSWORD': 'new'}):
            self.assertEqual(versa_credentials(self.root, 'versavnf'), ('admin', 'new', 'new'))
        for value in ('', 'password\ncommand', 'pass\x7f'):
            with patch.dict('os.environ', {'VERSA_PASSWORD': value}):
                with self.assertRaises(ValueError):
                    versa_credentials(self.root, 'versafvnf')

    def test_command_file_rules_and_comments(self):
        path = self.base / 'node-init.cfg'
        path.write_text('# credentials only\n')
        self.assertEqual(config_commands(path, 'versafvnf'), [])
        path.write_text('set interfaces vni-0/0 description WAN\ndelete system ntp server old.example\n')
        self.assertEqual(len(config_commands(path, 'versafvnf')), 2)
        for command in ('commit', 'configure', 'sudo passwd admin', 'set system x; commit',
                        'set system x | display set', 'set system x?', 'set system x\x1b'):
            path.write_text(command)
            with self.assertRaises(ValueError):
                config_commands(path, 'versaana')

    @patch('eve_lab.initialize.paramiko.SSHClient')
    @patch('eve_lab.initialize.versa_credentials', side_effect=ValueError('Set VERSA_PASSWORD in .env'))
    @patch('eve_lab.initialize.load_server', return_value={})
    def test_missing_credentials_fail_before_host_connection(self, server, creds, ssh):
        self.node('versafvnf')
        with self.assertRaisesRegex(ValueError, 'VERSA_PASSWORD'):
            initialize(self.client, {'name': 'test'}, self.root, 'default')
        ssh.assert_not_called()

    @patch('eve_lab.initialize.VersaHeadendConsole')
    @patch('eve_lab.initialize.VersaConsole')
    @patch('eve_lab.initialize.paramiko.SSHClient')
    @patch('eve_lab.initialize.versa_credentials', return_value=('admin', 'desired', 'current'))
    @patch('eve_lab.initialize.load_server', return_value={
        'url': 'http://example.test', 'ssh_username': 'root', 'ssh_password': 'ssh-pass'})
    def test_dispatch_and_failure_reporting(self, server, creds, ssh, vos, headend):
        for template, factory in (('versafvnf', vos), ('versadir', headend), ('versaana', headend)):
            with self.subTest(template=template):
                self.node(template)
                result = initialize(self.client, {'name': 'test'}, self.root, 'default')
                self.assertEqual(result['completed'], ['node'])
                factory.return_value.login.assert_called_with('admin', 'desired', 'current')
                factory.return_value.initialize.assert_called_with([], username='admin', password='desired')
        headend.return_value.initialize.side_effect = RuntimeError('Versa commit was not confirmed')
        result = initialize(self.client, {'name': 'test'}, self.root, 'default')
        self.assertEqual(result['completed'], [])
        self.assertEqual(result['failed'][0]['node'], 'node')


if __name__ == '__main__':
    unittest.main()
