"""Versa 22.1.x serial initialization through the EVE host."""
import json
import re
import shlex
import sys

from .device_console import Console, credentials, environment_values


VERSA_PREFIXES = {
    'versafvnf': 'VERSA',
    'versavnf': 'VERSA',
    'versadir': 'VERSA_DIRECTOR',
    'versaana': 'VERSA_ANALYTICS',
}

# Shells can use either user@host:~$ or [user@host: ~] $.
SHELL = r'^\[?[\w.-]+@[\w.-]+:[^\n]*?[\$#]\s*$'
CLI = r'^[\w.-]+@[\w.-]+(?:\([^\n)]*\))?[>%]\s*$'
LOGIN = r'(?i:^[^\n]*login:\s*$|^username:\s*$|^password:\s*$)'
PASSWORD = r'(?i:^(?:enter )?(?:\(current\) UNIX |current UNIX |current |old |new UNIX |new |retype new UNIX |retype new |confirm new |confirm )password:\s*$)'
FAILURE = r'(?i:^.*(?:login incorrect|authentication failed|authentication token manipulation error|password unchanged|passwords do not match|bad password).*$)'
WIZARD = r'(?i:^.*(?:do you want to|would you like to|enter your selection).*$)'
OPTIONAL_SETUP = r'(?i:^\s*do you want to enter setup\s*\?\s*(?:\((?:yes/no|y/n)\)|\[(?:yes/no|y/n)\])?\s*:?\s*$)'


def versa_credentials(root, template):
    prefix = VERSA_PREFIXES[template]
    username, password = credentials(root, prefix)
    current = environment_values(root).get(prefix + '_CURRENT_PASSWORD') or password
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]*', username):
        raise ValueError('Invalid ' + prefix + '_USERNAME')
    for value in (password, current):
        if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError(prefix + ' passwords must be nonempty without control characters')
    return username, password, current


def validate_commands(commands):
    for line in commands:
        if not line.startswith(('set ', 'delete ')) or any(c in line for c in '\n\r;|?`'):
            raise ValueError('Versa init files require single-line configuration-mode set/delete commands; '
                             'no shell commands, pipes, semicolons, or interactive commands')


class VersaConsole(Console):
    """VOS uses CLI users; headends change the existing shell account with passwd."""

    headend = False
    sudo_password = False

    def _prompt(self, prompt, username=None):
        prompt = prompt.strip()
        if username and not re.match(r'^\[?' + re.escape(username) + r'@', prompt):
            raise RuntimeError('Versa console belongs to a different user; log out before initialization')
        if re.fullmatch(SHELL, prompt):
            self.mode = 'shell'
        elif prompt.endswith('%'):
            raise RuntimeError('Versa console is already in configuration mode; exit it before initialization')
        else:
            self.mode = 'cli'
        self.prompt = prompt

    def login(self, username, password, current_password):
        self.username = username
        self.current_password = current_password
        self.password_changed = False
        self.send('')
        seen = set()
        discarded_stale_failure = False
        declined_setup = False
        pattern = '|'.join((LOGIN, PASSWORD, FAILURE, WIZARD, SHELL, CLI))
        for _ in range(12):
            output, match = self.expect(pattern, timeout=self.boot_timeout, wake=not seen)
            prompt = match.group().strip()
            lower = prompt.lower()
            if re.search(FAILURE, output, re.M):
                if not seen and not discarded_stale_failure and re.search(
                        r'login incorrect|authentication failed', prompt, re.I):
                    discarded_stale_failure = True
                    print('Discarding an earlier Versa console login failure; waiting for a fresh login prompt...',
                          file=sys.stderr, flush=True)
                    continue
                if {'current', 'new', 'confirm'} & seen:
                    raise RuntimeError('Versa rejected the first-login password change; check the new password policy '
                                       'and current password. No init-file commands applied (secrets omitted)')
                raise RuntimeError('Versa login rejected the current credentials. Set the matching *_CURRENT_PASSWORD '
                                   'in .env to the existing device password; when blank, login uses *_PASSWORD. '
                                   'Verify the existing username in the console. No init-file commands applied (secrets omitted)')
            if re.fullmatch(WIZARD, prompt):
                if self.headend and re.fullmatch(OPTIONAL_SETUP, prompt):
                    if declined_setup:
                        raise RuntimeError('Versa repeated the optional setup prompt after answering N; inspect the console')
                    declined_setup = True
                    print('Declining optional Versa setup; continuing to the shell...',
                          file=sys.stderr, flush=True)
                    self.send('N')
                    continue
                raise RuntimeError('Complete the Versa first-boot setup wizard in the console before running init')
            if re.fullmatch(SHELL, prompt) or re.fullmatch(CLI, prompt):
                if {'new', 'confirm'} & seen:
                    if not {'new', 'confirm'} <= seen:
                        raise RuntimeError('Versa first-login password change was not confirmed')
                    self.password_changed = True
                    self.current_password = password
                self._prompt(prompt, username)
                return
            if 'retype' in lower or 'confirm' in lower:
                stage, value = 'confirm', password
            elif lower.startswith(('new', 'enter new')):
                stage, value = 'new', password
            elif 'current' in lower or lower.startswith('old'):
                stage, value = 'current', current_password
            elif lower.endswith(('login:', 'username:')):
                stage, value = 'username', username
            else:
                stage, value = 'password', current_password
            if stage in seen:
                raise RuntimeError('Versa repeated an authentication prompt; no credentials retried')
            seen.add(stage)
            self.send(value)
        raise RuntimeError('Versa login did not complete')

    def command(self, command, timeout=60):
        self.send(command)
        output, match = self.expect(CLI, timeout=timeout)
        lines = output[:match.start()].splitlines()
        if lines and lines[0].strip() == command:
            lines.pop(0)
        output = '\n'.join(lines)
        if re.search(r'(?im)^\s*(?:error\b|syntax error\b|invalid\b|unknown command\b|'
                     r'incomplete command\b|aborted\b|failed\b|commit failed\b)', output):
            raise RuntimeError('Versa rejected a command; inspect the console (output omitted)')
        self.prompt = match.group().strip()
        return output

    def enter_cli(self):
        if self.mode == 'shell':
            self.command('cli', timeout=self.boot_timeout)
            self.mode = 'cli'
        if not self.prompt.endswith('>'):
            raise RuntimeError('Expected Versa operational CLI prompt before initialization')

    def change_shell_password(self, password):
        if self.current_password == password or self.password_changed:
            return
        if self.mode != 'shell':
            raise RuntimeError('Director/Analytics password changes require a shell login; exit CLI and retry')
        # No password in a shell command, process argument, or temporary file.
        self.send('sudo passwd ' + shlex.quote(self.username) if self.sudo_password else 'passwd')
        sudo_prompt = r'(?i:^\[sudo\] password for ' + re.escape(self.username) + r':\s*$)'
        seen = set()
        transcript = ''
        for _ in range(6):
            output, match = self.expect('|'.join((sudo_prompt, PASSWORD, FAILURE, SHELL)),
                                        timeout=self.boot_timeout)
            transcript += output
            prompt = match.group().strip()
            lower = prompt.lower()
            if re.search(FAILURE, output, re.M):
                raise RuntimeError('Versa shell password change failed; inspect password policy (secrets omitted)')
            if re.fullmatch(SHELL, prompt):
                if not {'new', 'confirm'} <= seen or not re.search(
                        r'password updated successfully', transcript, re.I):
                    raise RuntimeError('Versa shell password change was not confirmed')
                self.password_changed = True
                self.current_password = password
                self._prompt(prompt, self.username)
                return
            if re.fullmatch(sudo_prompt, prompt):
                stage, value = 'sudo', self.current_password
            elif 'retype' in lower or 'confirm' in lower:
                stage, value = 'confirm', password
            elif lower.startswith(('new', 'enter new')):
                stage, value = 'new', password
            else:
                stage, value = 'current', self.current_password
            if stage in seen:
                raise RuntimeError('Versa repeated a password-change prompt; inspect password policy')
            seen.add(stage)
            self.send(value)
        raise RuntimeError('Versa shell password change did not complete')

    def initialize(self, commands, username=None, password=None):
        validate_commands(commands)
        if self.headend:
            self.change_shell_password(password)
            if not commands:
                return
        self.enter_cli()
        self.command('configure')
        if not self.prompt.endswith('%'):
            raise RuntimeError('Versa did not enter configuration mode')
        for command in commands:
            self.command(command, timeout=self.boot_timeout)
        if not self.headend:
            # ConfD quoted strings preserve spaces and punctuation in passwords.
            self.command('set system users ' + username + ' password ' +
                         json.dumps(password))
        output = self.command('commit', timeout=self.boot_timeout)
        if not re.search(r'(?im)^\s*(?:Commit complete\.?|No modifications to commit\.?)\s*$', output):
            raise RuntimeError('Versa commit was not confirmed; inspect candidate configuration before retrying')
        self.command('exit')


class VersaHeadendConsole(VersaConsole):
    headend = True
