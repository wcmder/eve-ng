"""Repository configuration and local credentials."""

import os
from pathlib import Path

import yaml


def load_server(root: Path, name: str, auth: str = "web") -> dict:
    """Read simple KEY=value credentials; shell environment takes precedence."""
    env = dict(os.environ)
    env_file = root / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                key, separator, value = line.partition("=")
                if not separator:
                    raise ValueError("Invalid .env entry; expected KEY=value")
                env.setdefault(key.strip(), value.strip())
    config = yaml.safe_load((root / "config/servers.yaml").read_text())
    try:
        server = dict(config["servers"][name])
    except KeyError:
        raise ValueError(f"Unknown server: {name}") from None
    fields = ("ssh_username", "ssh_password") if auth == "ssh" else ("username", "password")
    for field in fields:
        variable = server.get(f"{field}_env", f"EVE_{field.upper()}")
        if not env.get(variable):
            raise ValueError(f"Set {variable} in the environment or .env")
        server[field] = env[variable]
    return server
