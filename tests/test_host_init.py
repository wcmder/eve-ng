import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from eve_lab.host_init import initialize_host, change_linux_password


class HostInitTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / 'config').mkdir()
        (self.root / 'config/servers.yaml').write_text('servers:\n  default:\n    url: http://host\n')
        (self.root / '.env').write_text('EVE_USERNAME=admin\nEVE_PASSWORD=new-web\n'
                                      'EVE_CURRENT_PASSWORD=old-web\nEVE_SSH_USERNAME=root\n'
                                      'EVE_SSH_PASSWORD=new-ssh\nEVE_SSH_CURRENT_PASSWORD=old-ssh\n')

    @patch('eve_lab.host_init.ssh_login')
    @patch('eve_lab.host_init.EveClient')
    def test_preview_does_not_connect_or_expose_secrets(self, api, ssh):
        result = initialize_host(self.root, check=True)
        self.assertEqual(result['planned'], ['web', 'ssh'])
        self.assertNotIn('old-web', str(result))
        api.assert_not_called()
        ssh.assert_not_called()

    @patch('eve_lab.host_init.change_linux_password')
    @patch('eve_lab.host_init.ssh_login')
    @patch('eve_lab.host_init.EveClient')
    def test_rotates_and_verifies_both_accounts(self, api, ssh, change):
        old, new = MagicMock(), MagicMock()
        api.side_effect = [old, new]
        old.request.return_value = {'admin': {'name': 'Admin', 'role': 'admin', 'password': 'hash', 'ip': 'host', 'pod': -1, 'pexpiration': -1, 'expiration': -1}}
        result = initialize_host(self.root)
        old.login.assert_called_once_with('admin', 'old-web', html5=False)
        old.request.assert_any_call('PUT', 'users/admin', {'password': 'new-web'})
        new.login.assert_called_once_with('admin', 'new-web', html5=False)
        self.assertEqual([c.args[1] for c in ssh.call_args_list], ['old-ssh', 'new-ssh'])
        change.assert_called_once_with(ssh.return_value, 'old-ssh', 'new-ssh')
        self.assertEqual(result['verified'], ['web', 'ssh'])
        self.assertEqual(result['failed'], [])

    @patch('eve_lab.host_init.change_linux_password')
    @patch('eve_lab.host_init.ssh_login')
    @patch('eve_lab.host_init.EveClient')
    def test_failed_preflight_changes_neither_account(self, api, ssh, change):
        api.return_value.request.return_value = {'admin': {}}
        ssh.side_effect = RuntimeError('secret details')
        result = initialize_host(self.root)
        self.assertEqual(result['changed'], [])
        self.assertFalse(any(c.args[0] == 'PUT' for c in api.return_value.request.call_args_list))
        change.assert_not_called()
        self.assertNotIn('secret details', str(result))

    @patch('eve_lab.host_init.change_linux_password', side_effect=RuntimeError('secret'))
    @patch('eve_lab.host_init.ssh_login')
    @patch('eve_lab.host_init.EveClient')
    def test_partial_failure_reports_web_success(self, api, ssh, change):
        api.return_value.request.return_value = {'admin': {}}
        result = initialize_host(self.root)
        self.assertEqual(result['changed'], ['web'])
        self.assertEqual(result['verified'], ['web'])
        self.assertEqual(result['failed'][0]['account'], 'ssh')

    @patch('eve_lab.host_init.EveClient')
    def test_web_login_failure_is_specific_and_redacted(self, api):
        from eve_lab.client import EveAPIError
        api.return_value.login.side_effect = EveAPIError('old-web new-web', 401)
        result = initialize_host(self.root)
        failure = result['failed'][0]
        self.assertIn('web login rejected', failure['reason'])
        self.assertIn('No password changes were attempted', failure['reason'])
        self.assertNotIn('old-web', str(result))
        api.return_value.request.assert_not_called()

    @patch('eve_lab.host_init.EveClient')
    def test_user_lookup_failure_is_distinct_from_login(self, api):
        from eve_lab.client import EveAPIError
        api.return_value.request.side_effect = EveAPIError('sensitive body', 403)
        result = initialize_host(self.root)
        failure = result['failed'][0]
        self.assertEqual(failure['stage'], 'reading web account settings')
        self.assertIn('permissions', failure['reason'])
        self.assertIn('No password changes were attempted', failure['reason'])
        api.return_value.request.assert_called_once_with('GET', 'users/')
        self.assertNotIn('sensitive body', str(result))

    @patch('eve_lab.host_init.Console')
    def test_passwd_prompts_no_secret_in_command(self, console):
        prompts = iter(['(current) UNIX password:', 'New password:', 'Retype new password:',
                        'passwd: password updated successfully'])
        def expect(pattern, **kwargs):
            text = next(prompts)
            match = re.search(pattern, text, re.M)
            self.assertIsNotNone(match)
            return text, match
        console.return_value.expect.side_effect = expect
        ssh = MagicMock()
        change_linux_password(ssh, 'old', 'new')
        ssh.get_transport.return_value.open_session.return_value.exec_command.assert_called_once_with('passwd')
        self.assertEqual([c.args[0] for c in console.return_value.send.call_args_list], ['old', 'new', 'new'])

    @patch('eve_lab.cli.initialize_host', return_value={'failed': []})
    def test_cli_host_needs_no_lab(self, init):
        from eve_lab.cli import main
        with patch('sys.argv', ['eve', '--root', str(self.root), 'init', '--host', 'all', '--check']), patch('builtins.print'):
            main()
        init.assert_called_once_with(self.root, 'default', 'all', True)
