# EVE-NG labs

Shared Python tooling for `http://10.0.4.4`, with independent lab definitions.

## Setup

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

The local, gitignored `.env` contains the supplied credentials. For another checkout,
copy `.env.example` to `.env` and fill in the password. Shell environment variables
override `.env`. Server addresses belong in `config/servers.yaml`.

## Commands

Run from the repository root with the virtual environment activated:

| Command | Purpose | Contacts EVE-NG? |
| --- | --- | --- |
| `eve plan <lab>` | Validate the local YAML and summarize object counts. | No |
| `eve apply <lab>` | Create a missing lab, networks, nodes, and connections; reuse matching objects. | Yes |
| `eve start <lab>` | Start the nodes declared in the YAML, skipping those already running. | Yes |
| `eve stop <lab>` | Stop the nodes declared in the YAML, skipping those already stopped. | Yes |
| `eve delete <lab>` | Stop all remote nodes and permanently delete the entire remote lab. | Yes |
| `eve status [lab]` | Read server statistics, or a lab's nodes and networks when a lab is provided. | Yes |
| `eve templates` | List available device templates. | Yes |
| `eve template <name>` | Fetch template details, image options, and server defaults. | Yes |

All commands print JSON. Remote commands log in and log out using an in-memory
session cookie. Discovery, status, and local plan commands do not modify devices.
The current implementation loads server configuration and credentials even for
`plan`. Starting a VM does not mean the guest OS has finished booting.

### Deploy a lab

```sh
eve template c8000v
eve plan palo-lab
eve apply palo-lab
eve start palo-lab
eve status palo-lab
# When finished:
eve stop palo-lab
```

`apply` validates the YAML, checks template/image availability and network types,
and requires `remote_folder` to exist on EVE-NG. It creates the lab if missing,
adds missing objects, and connects interfaces. Nodes use template defaults with
YAML settings overriding them. Interfaces are resolved from EVE-NG's returned
names and IDs; `GigabitEthernet1`, `Gi1`, and `g1` are equivalent spellings.
`apply` does not start nodes or push guest device configurations from `configs/`.

Reruns match objects by name within the remote lab and skip matching objects and
connections. Existing settings that differ from explicitly declared YAML settings
cause a conflict; this version does not resize, replace, delete, or rewire existing
objects. Manually adjusted canvas positions are preserved. Stop a node before
adding a connection. Objects absent from the YAML are left alone, and `start` and
`stop` act only on declared nodes. Run one deployment at a time per remote lab.

EVE-NG has no transaction around these operations. A failed apply may leave a
partially created lab; the error lists completed operations. Inspect the lab,
resolve the error, and rerun. New-node interface names can only be verified after
node creation. No automatic rollback or deletion is performed.

### Delete a lab

```sh
eve delete palo-lab
eve delete palo-lab --server default
```

`delete` uses the lab name and `remote_folder` in the local topology to select the
remote `.unl` file. It stops every active node in that lab, including nodes added
manually that are absent from the YAML, then deletes the whole remote lab and
verifies it is gone. This removes its nodes, networks, and saved lab configuration.
Local YAML and `configs/` files remain available to recreate it with `eve apply`.

Deletion executes immediately without an interactive prompt. If the lab is already
absent, the command succeeds without changes. If a node cannot be stopped, deletion
aborts; nodes stopped earlier remain stopped. Failures report partial progress.
The local topology file must still exist and pass validation.

### Deployment errors

HTTP failures include the server's JSON error message when available. A timeout
identifies the request and does not imply that the server cancelled it. The default
server timeout is 120 seconds, configured in `config/servers.yaml`.

If EVE-NG reports `Failed to lock the lab (60061)`, stop retrying writes and inspect
the server's PHP/web error logs and lab lock. A failed server request can leave a
`.unl.lock` file behind. Confirm no active operation owns it before recovering the
lock, then rerun `eve apply`; existing matching lab objects will be reused.

The server's API error log is `/opt/unetlab/data/Logs/api.txt`. For example,
`Undefined array key "left"` indicates missing node coordinates. The client sends
default `left: 200` and `top: 200` when the YAML omits them; explicit positions
override these defaults. Node creation also sends `numberNodes: 1` and a blank
UUID, matching the web UI's single-node request.

### Options and help

```sh
eve --help
eve apply --help
eve template --help
eve apply palo-lab --server default
eve status palo-lab --server default
eve --root /path/to/eve-ng plan palo-lab
```

`--server <name>` follows the subcommand and selects an entry in
`config/servers.yaml`; it defaults to `default` (`http://10.0.4.4`). `--root <path>`
goes before the subcommand and defaults to the current working directory. Activate
the project's virtual environment before using `eve`, or invoke `.venv/bin/eve`
from the repository root.

## Topology format

Each lab lives in `labs/<name>/` with a `topology.yaml` and `configs/`.
Keep usage instructions and lab-specific notes in this root README.
The topology's `name` must match its directory. This is the project's declarative
format, not a native EVE-NG import file. Deployment currently supports QEMU nodes
and Ethernet links from nodes to networks.

```yaml
name: palo-lab
description: Initial C8000V uplink for the Palo Alto lab
remote_folder: /
nodes:
  - name: R1
    template: c8000v
    type: qemu
    image: c8000v-17.15.06
    cpu: 4
    ethernet: 4
networks:
  - name: mgmt
    type: pnet1
links:
  - node: R1
    interface: GigabitEthernet1
    network: mgmt
```

Use the image directory's basename: `/opt/unetlab/addons/qemu/c8000v-17.15.06`
becomes `c8000v-17.15.06`. `cpu` is the vCPU count, `ethernet` is the Ethernet
interface count, and optional `ram` is RAM in MB. Optional node fields also include
`console`, `left`, and `top`; networks accept optional `left` and `top` positions.
Changing these resource declarations after deployment currently reports a conflict
on apply; automatic updates are not implemented.

Add entries under `links` to connect additional interfaces; increasing `ethernet`
alone does not connect them. Place guest configuration templates in each lab's
`configs/` directory and keep secrets out of templates.

`plan` checks required fields, unique names, positive resource counts, link
references, and duplicate interface assignments. It does not contact the server,
validate hardware capacity or guest compatibility, or compare the remote lab.

## Labs

### palo-lab

The initial router for the Palo Alto lab is defined in
[labs/palo-lab/topology.yaml](labs/palo-lab/topology.yaml), targeting `default`
(`http://10.0.4.4`). C8000V `R1` uses image `c8000v-17.15.06`, 4 vCPUs, and
4 Ethernet interfaces. GigabitEthernet1 connects to `mgmt`, backed by `pnet1`.
The image was confirmed in the server's template options. RAM and other omitted
settings use the server's template defaults on creation.

`eve plan palo-lab` reports 1 node, 1 network, and 1 link. `eve apply palo-lab`
creates `/palo-lab.unl` if missing and adds the router, network, and connection.
Use `eve start palo-lab` to boot R1 and `eve stop palo-lab` to stop it.
`eve status palo-lab` returns the remote nodes and networks, including R1's status
and console URL when available. Follow the [deployment steps](#deploy-a-lab)
for the full command sequence and rerun behavior.

## Structure

- `README.md`: shared command usage and notes for all labs.
- `src/eve_lab/`: shared configuration, API client, topology validation, deployment, and CLI.
- `config/servers.yaml`: named server connections.
- `labs/palo-lab/`: first lab's definition and device configurations.
- `.state/`: reserved for future generated state; deployment currently reads remote objects directly.
- `tests/`: deployment, validation, and API error tests using a simulated server.

## Verification

```sh
python -m unittest discover -s tests -v
```

API references: [EVE-NG API](https://www.eve-ng.net/index.php/how-to-eve-ng-api/)
and [evengsdk interface connection implementation](https://ttafsir.github.io/evengsdk/api_reference/).
