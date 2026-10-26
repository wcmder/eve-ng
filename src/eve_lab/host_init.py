"""Rotate the configured EVE web and Linux account passwords explicitly."""
import re
from urllib.parse import quote, urlsplit

import paramiko

from .client import EveClient, EveAPIError
from .config import load_server
from .device_console import Console, environment_values


def settings(root, server_name, kind):
    server = load_server(root, server_name, auth=kind)
    field = 'ssh_password' if kind == 'ssh' else 'password'
    variable = server.get(field + '_env', 'EVE_' + field.upper())
    if not variable.endswith('_PASSWORD'):
        raise ValueError('Host password initialization requires password_env names ending in _PASSWORD')
    current_variable = variable[:-len('_PASSWORD')] + '_CURRENT_PASSWORD'
    desired = server[field]
    current = environment_values(root).get(current_variable) or desired
    for value in (desired, current):
        if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError('Host passwords must be nonempty without control characters')
    return server, current, desired


def ssh_login(server, password):
    ssh = paramiko.SSHClient()
    try:
        ssh.load_system_host_keys()
        ssh.connect(server.get('ssh_host') or urlsplit(server['url']).hostname,
                    username=server['ssh_username'], password=password,
                    timeout=15, auth_timeout=15, banner_timeout=15,
                    allow_agent=False, look_for_keys=False)
        return ssh
    except Exception:
        ssh.close()
        raise


def change_linux_password(ssh, current, desired):
    channel = ssh.get_transport().open_session(timeout=15)
    try:
        channel.get_pty(term='vt100')
        channel.exec_command('passwd')
        console = Console(channel)
        pattern = (r'(?im:^.*password updated successfully.*$|'
                   r'^.*(?:authentication token manipulation error|password unchanged|bad password|'
                   r'passwords do not match|failure|error|must .*password).*$|'
                   r'^(?:\(current\) UNIX |current UNIX |current |old |new UNIX |new |retype new UNIX |retype new )password:\s*$)')
        seen = set()
        for _ in range(5):
            _, match = console.expect(pattern, timeout=60)
            prompt = match.group().strip().lower()
            if 'password updated successfully' in prompt:
                if not {'new', 'confirm'} <= seen:
                    raise RuntimeError('Password update was not confirmed')
                return
            if not prompt.endswith('password:'):
                raise RuntimeError('Linux rejected password change')
            stage = 'confirm' if prompt.startswith('retype') else 'new' if prompt.startswith('new') else 'current'
            if stage in seen:
                raise RuntimeError('Linux repeated a password prompt')
            seen.add(stage)
            console.send(current if stage == 'current' else desired)
        raise RuntimeError('Linux password change did not complete')
    finally:
        channel.close()


def failure_reason(error, kind, stage):
    """Explain failures without exposing API responses or authentication secrets."""
    if isinstance(error, EveAPIError):
        code = error.code
        if stage == 'authenticating existing credentials' and code in (401, 403):
            return ('EVE web login rejected. Check EVE_USERNAME and EVE_CURRENT_PASSWORD; '
                    'if current is blank, login uses EVE_PASSWORD. Shell environment overrides .env.')
        if code in (401, 403):
            return 'EVE API denied this operation; check session validity and user-management permissions.'
        if code == 404:
            return 'EVE API endpoint was not found; check the server URL and API compatibility.'
        return 'EVE API request failed' + (f' (code {code})' if code is not None else '') + '.'
    if isinstance(error, paramiko.BadHostKeyException):
        return 'EVE SSH host key changed; verify it through a trusted console before updating known_hosts.'
    if isinstance(error, paramiko.AuthenticationException):
        return 'EVE SSH login rejected; check EVE_SSH_USERNAME and the selected current/desired password.'
    if str(error) == 'Configured EVE user was not returned':
        return 'Web login succeeded, but the user listing did not contain the configured account.'
    if isinstance(error, TimeoutError) or str(error).startswith('Timed out'):
        return 'Timed out waiting for EVE; check connectivity and service readiness.'
    if str(error).startswith('Cannot reach EVE-NG') or isinstance(error, OSError):
        return 'Cannot reach EVE; check the configured host, port, and service availability.'
    if kind == 'web' and stage == 'authenticating existing credentials':
        return 'EVE web login failed; check server connectivity and the selected current password.'
    return 'Host password operation failed; check account permissions, password policy, and trusted SSH host keys.'


def initialize_host(root, server_name='default', target='all', check=False):
    kinds = ['web', 'ssh'] if target == 'all' else [target]
    if any(kind not in ('web', 'ssh') for kind in kinds):
        raise ValueError('Host target must be web, ssh, or all')
    configs = {kind: settings(root, server_name, kind) for kind in kinds}
    result = {'server': server_name, 'check': check, 'planned': kinds,
              'changed': [], 'verified': [], 'failed': []}
    if check:
        return result
    web = ssh = None
    stage = 'authenticating existing credentials'
    kind = kinds[0]
    change_attempted = False
    try:
        # Authenticate both accounts before changing either one.
        payload = None
        if 'web' in configs:
            kind = 'web'
            server, current, desired = configs[kind]
            web = EveClient(server['url'], server.get('timeout', 15))
            web.login(server['username'], current, html5=False)
            stage = 'reading web account settings'
            users = web.request('GET', 'users/')
            user = users.get(server['username']) if isinstance(users, dict) else None
            if not isinstance(user, dict):
                raise RuntimeError('Configured EVE user was not returned')
            # GET user metadata is presentation data, not a safe PUT payload.
            # In CE, POD 0 may be reported as -1; resending it rewrites the POD.
            payload = {'password': desired}
        if 'ssh' in configs:
            kind = 'ssh'
            server, current, desired = configs[kind]
            stage = 'authenticating existing credentials'
            ssh = ssh_login(server, current)
        for kind in kinds:
            server, current, desired = configs[kind]
            stage = 'changing password'
            if current != desired:
                change_attempted = True
                if kind == 'web':
                    web.request('PUT', 'users/' + quote(server['username'], safe=''), payload)
                else:
                    change_linux_password(ssh, current, desired)
                result['changed'].append(kind)
            stage = 'verifying new login'
            if kind == 'web':
                verify = EveClient(server['url'], server.get('timeout', 15))
                try:
                    verify.login(server['username'], desired, html5=False)
                finally:
                    try:
                        verify.logout()
                    except Exception:
                        pass
            else:
                verify = ssh_login(server, desired)
                verify.close()
            result['verified'].append(kind)
    except (RuntimeError, ValueError, OSError, paramiko.SSHException) as error:
        state = ('Changes may already have applied; verify access before retrying.' if change_attempted
                 else 'No password changes were attempted.')
        result['failed'].append({'account': kind, 'stage': stage,
                                'reason': failure_reason(error, kind, stage) + ' ' + state})
    finally:
        if ssh is not None:
            ssh.close()
        if web is not None:
            try:
                web.logout()
            except Exception:
                pass
    return result
