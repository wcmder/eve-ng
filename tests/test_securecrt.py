import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from eve_lab.securecrt import generate


class SecureCRTTests(unittest.TestCase):
    def test_generated_script_creates_and_updates_existing(self):
        report = {'leases': [
            {'status': 'active', 'ip_address': '172.16.1.109', 'hostname': 'Router'},
            {'status': 'permanent', 'ip_address': '172.16.1.110', 'hostname': None},
            {'status': 'expired', 'ip_address': '172.16.1.111', 'hostname': 'old'},
            {'status': 'active', 'ip_address': '172.16.1.109', 'hostname': 'Router'},
        ]}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'sessions.py'
            result = generate(report, output, 'admin', 2222)
            self.assertEqual(result['session_count'], 2)
            saved = {}
            crt = MagicMock()
            def open_session(name):
                if name == 'Default':
                    config = MagicMock()
                    options = {}
                    config.SetOption.side_effect = options.__setitem__
                    config.Save.side_effect = lambda target: saved.update({target: dict(options)})
                    return config
                if name not in saved:
                    raise RuntimeError('Session does not exist')
                return MagicMock()
            crt.OpenSessionConfiguration.side_effect = open_session
            source = output.read_text()
            # SecureCRT treats consecutive leading # lines as header directives.
            self.assertEqual(source.splitlines()[:3], [
                '# $language = "Python"', '# $interface = "1.0"', 'import json'])
            exec(compile(source, str(output), 'exec'), {'crt': crt})
            self.assertEqual(saved['eve/Router - 172.16.1.109'], {
                'Protocol Name': 'SSH2', 'Hostname': '172.16.1.109',
                '[SSH2] Port': 2222, 'Username': 'admin',
                'Credential Title': '', 'Prompt For Credential Title': 0})
            exec(compile(source, str(output), 'exec'), {'crt': crt})
            self.assertIn('0 created, 2 updated', crt.Dialog.MessageBox.call_args.args[0])

    def test_untrusted_hostname_and_empty_report(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'sessions.py'
            result = generate({'leases': [{'status': 'active', 'ip_address': '10.0.0.1',
                                         'hostname': '../evil\\name\n"'}]}, output)
            name = result['sessions'][0]
            self.assertEqual(name.count('/'), 1)
            self.assertNotIn('\\', name)
            compile(output.read_text(), str(output), 'exec')
            result = generate({'leases': []}, output)
            self.assertEqual(result['session_count'], 0)
            with self.assertRaises(ValueError):
                generate({'leases': []}, output, port=0)

    def test_interactive_defaults_duplicates_and_custom_names(self):
        report = {'leases': [
            {'status': 'active', 'ip_address': '10.0.0.1', 'hostname': 'Router'},
            {'status': 'active', 'ip_address': '10.0.0.2', 'hostname': 'Router'},
            {'status': 'active', 'ip_address': '10.0.0.3', 'hostname': None},
        ]}
        with tempfile.TemporaryDirectory() as directory, patch('sys.stderr'):
            output = Path(directory) / 'sessions.py'
            with patch('builtins.input', side_effect=['', '', '']):
                result = generate(report, output, interactive=True)
            self.assertEqual(result['sessions'], ['eve/Router', 'eve/Router - 10.0.0.2', 'eve/10.0.0.3'])
            with patch('builtins.input', side_effect=['R1', 'r1', '../PA1', 'PA1', 'Other']):
                result = generate(report, output, interactive=True)
            self.assertEqual(result['sessions'], ['eve/R1', 'eve/PA1', 'eve/Other'])

    def test_interactive_cancel_preserves_output(self):
        report = {'leases': [{'status': 'active', 'ip_address': '10.0.0.1', 'hostname': 'Router'}]}
        with tempfile.TemporaryDirectory() as directory, patch('sys.stderr'):
            output = Path(directory) / 'sessions.py'
            output.write_text('existing')
            for error in (EOFError, KeyboardInterrupt):
                with patch('builtins.input', side_effect=error):
                    with self.assertRaisesRegex(ValueError, 'cancelled'):
                        generate(report, output, interactive=True)
                self.assertEqual(output.read_text(), 'existing')

    def test_named_credentials_reference_without_password(self):
        report = {'leases': [{'status': 'active', 'ip_address': '10.0.0.1', 'hostname': 'R1'}]}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'sessions.py'
            generate(report, output, credentials='eve-default')
            crt = MagicMock()
            config = MagicMock()
            def open_session(name):
                if name == 'Default':
                    return config
                raise RuntimeError('missing')
            crt.OpenSessionConfiguration.side_effect = open_session
            exec(compile(output.read_text(), str(output), 'exec'), {'crt': crt})
            config.SetOption.assert_any_call('Credential Title', 'eve-default')
            config.SetOption.assert_any_call('Prompt For Credential Title', 0)
            self.assertFalse(any(call.args[0] in ('Username', 'Password', 'Password V2')
                                 for call in config.SetOption.call_args_list))
            config.Save.assert_called_once_with('eve/R1 - 10.0.0.1')
            with self.assertRaisesRegex(ValueError, 'either --credentials or --username'):
                generate(report, output, username='admin', credentials='eve-default')
            with self.assertRaisesRegex(ValueError, 'must not be empty'):
                generate(report, output, credentials='')

    def test_existing_session_credentials_update_preserves_settings(self):
        report = {'leases': [{'status': 'active', 'ip_address': '10.0.0.1', 'hostname': 'R1'}]}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'sessions.py'
            generate(report, output, credentials='eve-default')
            options = {'Credential Title': 'old', 'Prompt For Credential Title': 1,
                       'Hostname': '10.0.0.99', '[SSH2] Port': 2222,
                       'Username': 'old-user', 'Color Scheme': 'custom'}
            expected = dict(options, **{'Credential Title': 'eve-default', 'Prompt For Credential Title': 0,
                                        'Protocol Name': 'SSH2', 'Hostname': '10.0.0.1', '[SSH2] Port': 22})
            crt = MagicMock()
            config = crt.OpenSessionConfiguration.return_value
            config.GetOption.side_effect = options.__getitem__
            config.SetOption.side_effect = options.__setitem__
            source = compile(output.read_text(), str(output), 'exec')
            exec(source, {'crt': crt})
            self.assertEqual(options, expected)
            config.Save.assert_called_once_with("eve/R1 - 10.0.0.1")
            self.assertIn('0 created, 1 updated', crt.Dialog.MessageBox.call_args.args[0])
            config.Save.reset_mock()
            exec(source, {'crt': crt})
            config.Save.assert_called_once_with("eve/R1 - 10.0.0.1")
            self.assertIn('0 created, 1 updated', crt.Dialog.MessageBox.call_args.args[0])
