import copy
import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError

from eve_lab.client import EveAPIError, EveClient
from eve_lab.deploy import apply, delete, lab_path, lifecycle
from eve_lab.topology import expand_links, load_lab_target, validate


class FakeEve:
    def __init__(self):
        self.exists = False
        self.nodes = {}
        self.networks = {}
        self.ports = {}
        self.writes = []
        self.images = {"c8000v-17.15.06": "c8000v-17.15.06"}
        self.fail_link = False
        self.list_ports = False

    def request(self, method, path, payload=None):
        if method in ("POST", "PUT", "DELETE") or path.endswith(("/start", "/stop")):
            self.writes.append((method, path, copy.deepcopy(payload)))
        if path.startswith("list/templates/"):
            return {"type": "qemu", "options": {
                "image": {"list": self.images}, "ram": {"value": 6144},
                "cpu": {"value": 2}, "ethernet": {"value": 4}}}
        if path == "list/networks":
            return {"pnet1": "Cloud1", "bridge": "bridge"}
        if path == "folders/":
            return {}
        if path == "labs" and method == "POST":
            self.exists = True
            return None
        base = "labs/palo-lab.unl"
        if path == base:
            if not self.exists:
                raise EveAPIError("missing", 404)
            if method == "DELETE":
                self.exists = False
                self.nodes.clear()
                self.networks.clear()
                self.ports.clear()
                return None
            return {"name": "palo-lab"}
        for kind, objects in (("nodes", self.nodes), ("networks", self.networks)):
            if path == f"{base}/{kind}":
                if method == "GET":
                    return copy.deepcopy(objects) if objects else []
                if kind == "nodes" and payload.get("numberNodes") != 1:
                    raise EveAPIError("Missing node count", 500)
                if kind == "nodes" and not {"left", "top"} <= payload.keys():
                    raise EveAPIError("Undefined array key: node position", 500)
                if kind == "networks" and payload["type"] == "bridge" and not payload.get("visibility"):
                    return None  # EVE-NG omits unused hidden bridges when saving.
                ident = str(len(objects) + 1)
                objects[ident] = {**payload, "id": int(ident)}
                if kind == "nodes":
                    objects[ident]["status"] = 0
                    # Deliberately nonzero/noncontiguous ID: never guess from Gi1.
                    self.ports[ident] = {"7": {"name": "Gi1", "network_id": 0}}
                return None
        if method == "DELETE" and "/networks/" in path:
            ident = path.split("/networks/")[1]
            del self.networks[ident]
            for ports in self.ports.values():
                for port in ports.values():
                    if str(port["network_id"]) == ident:
                        port["network_id"] = 0
            return None
        if method == "DELETE" and "/nodes/" in path and not path.endswith("/interfaces"):
            ident = path.split("/nodes/")[1]
            del self.nodes[ident]
            del self.ports[ident]
            return None
        if method == "PUT" and "/networks/" in path:
            self.networks[path.split("/networks/")[1]].update(payload)
            return None
        if "/nodes/" in path:
            if method == "PUT" and "/" not in path.split("/nodes/")[1]:
                if "name" not in payload:
                    raise EveAPIError("Cannot edit node: Node has not been modified (40016)", 400)
                self.nodes[path.split("/nodes/")[1]].update(payload)
                return None
            ident, action = path.split("/nodes/")[1].split("/")
            if action == "interfaces":
                if method == "GET":
                    ports = copy.deepcopy(self.ports[ident])
                    return {"ethernet": list(ports.values()) if self.list_ports else ports}
                if method == "DELETE":
                    raise EveAPIError("Request not valid (60027)", 400)
                if self.fail_link:
                    raise EveAPIError("connection failed", 500)
                for port, target in payload.items():
                    if str(target) == "0":
                        raise EveAPIError("Cannot link node, invalid network_id (20033)", 400)
                    self.ports[ident][port]["network_id"] = 0 if target == "" else int(target)
                return None
            if action in ("start", "stop"):
                self.nodes[ident]["status"] = 2 if action == "start" else 0
                return None
        raise AssertionError((method, path, payload))


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        # Keep deployment tests independent of the user's evolving lab files.
        self.topology = {
            "name": "palo-lab", "remote_folder": "/",
            "nodes": [{"name": "R1", "template": "c8000v", "type": "qemu",
                       "image": "c8000v-17.15.06", "cpu": 4, "ethernet": 4}],
            "networks": [{"name": "mgmt", "type": "pnet1"}],
            "links": [{"node": "R1", "interface": "GigabitEthernet1", "network": "mgmt"}],
        }
        self.client = FakeEve()

    def test_create_and_rerun_without_duplicates(self):
        apply(self.client, self.topology)
        self.assertEqual(self.client.nodes["1"]["cpu"], 4)
        self.assertEqual(self.client.nodes["1"]["ram"], 6144)
        self.assertEqual(self.client.nodes["1"]["ethernet"], 4)
        self.assertEqual(self.client.nodes["1"]["status"], 0)
        self.assertEqual(self.client.nodes["1"]["numberNodes"], 1)
        self.assertEqual(self.client.nodes["1"]["uuid"], "")
        self.assertEqual(self.client.nodes["1"]["left"], 200)
        self.assertEqual(self.client.nodes["1"]["top"], 200)
        self.assertEqual(self.client.ports["1"]["7"]["network_id"], 1)
        self.client.writes.clear()
        self.assertEqual(apply(self.client, self.topology)["changes"], [])
        self.assertEqual(self.client.writes, [])

    def test_explicit_node_coordinates_override_defaults(self):
        self.topology["nodes"][0].update(left=350, top=450)
        apply(self.client, self.topology)
        self.assertEqual(self.client.nodes["1"]["left"], 350)
        self.assertEqual(self.client.nodes["1"]["top"], 450)

    def test_unconnected_bridge_persists_and_is_reused(self):
        self.topology["networks"].append({"name": "internal", "type": "bridge"})
        apply(self.client, self.topology)
        self.assertEqual(self.client.networks["2"]["name"], "internal")
        self.assertEqual(self.client.networks["2"]["visibility"], 1)
        self.assertEqual(apply(self.client, self.topology)["changes"], [])

    def test_prune_removes_orphans_and_disconnects_retained_node(self):
        apply(self.client, self.topology)
        self.topology["nodes"].append({**self.topology["nodes"][0], "name": "extra"})
        apply(self.client, self.topology)
        self.topology["nodes"].pop()
        self.topology["networks"] = []
        self.topology["links"] = []
        self.assertEqual(apply(self.client, self.topology, prune=False)["changes"], [])
        apply(self.client, self.topology, prune=True)
        self.assertEqual(list(self.client.nodes), ["1"])
        self.assertEqual(self.client.networks, {})
        self.assertEqual(self.client.ports["1"]["7"]["network_id"], 0)
        self.assertTrue(self.client.exists)
        self.assertEqual(apply(self.client, self.topology, prune=True)["changes"], [])

    def test_default_prune_moves_management_to_new_port(self):
        apply(self.client, self.topology)
        self.client.ports['1']['14'] = {'name': 'Gi8', 'network_id': 0}
        self.topology['links'][0]['interface'] = 'GigabitEthernet8'
        apply(self.client, self.topology, prune=False)
        self.assertEqual(self.client.ports['1']['7']['network_id'], 1)
        result = apply(self.client, self.topology)
        self.assertEqual(self.client.ports['1']['7']['network_id'], 0)
        self.assertEqual(self.client.ports['1']['14']['network_id'], 1)
        self.assertTrue(any('disconnected stale link' in change for change in result['changes']))
        self.assertEqual(apply(self.client, self.topology)['changes'], [])

    def test_stale_disconnect_must_persist(self):
        apply(self.client, self.topology)
        self.topology['links'] = []
        original = self.client.request
        def request(method, path, payload=None):
            if method == 'PUT' and payload == {'7': ''}:
                return None
            return original(method, path, payload)
        self.client.request = request
        with self.assertRaisesRegex(RuntimeError, 'Server did not disconnect'):
            apply(self.client, self.topology)

    def test_prune_preserves_direct_link_bridge(self):
        topology = self.direct_topology()
        apply(self.client, topology)
        self.assertEqual(apply(self.client, topology, prune=True)["changes"], [])
        self.assertEqual(self.client.networks["1"]["visibility"], 0)

    def test_prune_running_node_is_preserved(self):
        apply(self.client, self.topology)
        self.client.nodes["1"]["status"] = 2
        self.client.writes.clear()
        result = apply(self.client, self.topology, prune=True)
        self.assertTrue(result['deferred'])
        self.assertEqual(self.client.writes, [])

    def test_mixed_apply_updates_stopped_and_keeps_running_drift(self):
        self.topology['nodes'].append(dict(self.topology['nodes'][0], name='R2'))
        apply(self.client, self.topology)
        self.client.nodes['1']['status'] = 2
        self.topology['nodes'][0]['ram'] = 4096
        self.topology['nodes'][1]['ram'] = 4096
        self.topology['networks'][0]['type'] = 'bridge'
        self.client.writes.clear()
        result = apply(self.client, self.topology)
        self.assertEqual(self.client.nodes['1']['ram'], 6144)
        self.assertEqual(self.client.nodes['2']['ram'], 4096)
        self.assertEqual(self.client.networks['1']['type'], 'pnet1')
        self.assertTrue(result['deferred'])
        self.assertEqual(len(self.client.writes), 1)
        self.assertTrue(self.client.writes[0][1].endswith('/nodes/2'))

    def test_prune_keeps_undeclared_running_node_and_network(self):
        self.topology['nodes'].append(dict(self.topology['nodes'][0], name='R2'))
        apply(self.client, self.topology)
        self.client.nodes['1']['status'] = 2
        desired = dict(self.topology, nodes=[], networks=[], links=[])
        apply(self.client, desired)
        self.assertEqual(set(self.client.nodes), {'1'})
        self.assertEqual(set(self.client.networks), {'1'})
        self.assertEqual(str(self.client.ports['1']['7']['network_id']), '1')

    def test_stopped_node_can_join_live_management_cloud(self):
        self.topology['nodes'].append(dict(self.topology['nodes'][0], name='pano'))
        apply(self.client, self.topology)
        self.client.nodes['1']['status'] = 2
        before = copy.deepcopy(self.client.ports['1'])
        self.topology['links'].append({'node': 'pano', 'interface': 'Gi1', 'network': 'mgmt'})
        self.client.writes.clear()
        apply(self.client, self.topology)
        self.assertEqual(str(self.client.ports['2']['7']['network_id']), '1')
        self.assertEqual(self.client.ports['1'], before)
        self.assertEqual(len(self.client.writes), 1)
        self.assertTrue(self.client.writes[0][1].endswith('/nodes/2/interfaces'))
        self.assertEqual(apply(self.client, self.topology)['changes'], [])

    def test_running_node_management_attachment_remains_deferred(self):
        self.topology['nodes'].append(dict(self.topology['nodes'][0], name='pano'))
        apply(self.client, self.topology)
        self.client.nodes['1']['status'] = 2
        self.client.nodes['2']['status'] = 2
        self.topology['links'].append({'node': 'pano', 'interface': 'Gi1', 'network': 'mgmt'})
        self.client.writes.clear()
        result = apply(self.client, self.topology)
        self.assertEqual(str(self.client.ports['2']['7']['network_id']), '0')
        self.assertEqual(self.client.writes, [])
        self.assertTrue(result['deferred'])

    def test_running_direct_link_peer_is_preserved(self):
        topology = self.direct_topology()
        apply(self.client, topology)
        before = copy.deepcopy(self.client.ports)
        self.client.nodes['1']['status'] = 2
        topology['links'] = []
        self.client.writes.clear()
        apply(self.client, topology)
        self.assertEqual(self.client.ports, before)
        self.assertEqual(self.client.writes, [])

    def test_apply_aborts_if_node_starts_before_write(self):
        apply(self.client, self.topology)
        self.topology['nodes'][0]['ram'] = 4096
        original = self.client.request
        reads = []
        def request(method, path, payload=None):
            if method == 'GET' and path.endswith('/nodes'):
                reads.append(path)
                if len(reads) >= 3:
                    self.client.nodes['1']['status'] = 2
            return original(method, path, payload)
        self.client.request = request
        self.client.writes.clear()
        with self.assertRaisesRegex(RuntimeError, 'running state changed'):
            apply(self.client, self.topology)
        self.assertEqual(self.client.writes, [])

    def test_prune_verifies_deletion(self):
        apply(self.client, self.topology)
        self.topology["networks"] = []
        self.topology["links"] = []
        original = self.client.request
        def request(method, path, payload=None):
            if method == "DELETE" and "/networks/" in path:
                return None
            return original(method, path, payload)
        self.client.request = request
        with self.assertRaisesRegex(RuntimeError, "Server did not delete network"):
            apply(self.client, self.topology, prune=True)

    def direct_topology(self):
        self.topology["nodes"].append({**self.topology["nodes"][0], "name": "R2"})
        self.topology["networks"] = []
        self.topology["links"] = [{"name": "cable", "from": {"node": "R1", "interface": "Gi1"},
                                   "to": {"node": "R2", "interface": "GigabitEthernet1"}}]
        return self.topology

    def test_direct_link_create_hide_and_repeat(self):
        topology = self.direct_topology()
        apply(self.client, topology)
        self.assertEqual(self.client.networks["1"]["visibility"], 0)
        self.assertEqual(self.client.ports["1"]["7"]["network_id"], 1)
        self.assertEqual(self.client.ports["2"]["7"]["network_id"], 1)
        self.assertEqual(apply(self.client, topology)["changes"], [])

    def test_direct_link_reuses_visible_bridge_without_rewiring(self):
        topology = self.direct_topology()
        expanded, _ = expand_links(topology)
        apply(self.client, expanded)
        self.client.writes.clear()
        apply(self.client, topology)
        self.assertEqual(self.client.writes, [("PUT", "labs/palo-lab.unl/networks/1", {"visibility": 0})])

    def test_direct_link_rejects_shared_bridge(self):
        topology = self.direct_topology()
        expanded, _ = expand_links(topology)
        apply(self.client, expanded)
        self.client.nodes["3"] = {"name": "extra", "status": 0}
        self.client.ports["3"] = {"0": {"name": "eth0", "network_id": 1}}
        self.client.writes.clear()
        with self.assertRaisesRegex(RuntimeError, "other attached"):
            apply(self.client, topology, prune=False)
        self.assertEqual(self.client.writes, [])

    def test_prune_moves_direct_endpoint_and_management(self):
        topology = self.direct_topology()
        # Start with Gi2 as the cable endpoint; Gi1 is management.
        self.client.exists = False
        expanded, _ = expand_links(topology)
        apply(self.client, expanded)
        self.client.ports['1']['8'] = {'name': 'Gi2', 'network_id': 1}
        self.client.ports['1']['7']['network_id'] = 2
        self.client.networks['2'] = {'id': 2, 'name': 'mgmt', 'type': 'pnet1', 'visibility': 1}
        self.client.ports['1']['14'] = {'name': 'Gi8', 'network_id': 0}
        topology['networks'] = [{'name': 'mgmt', 'type': 'pnet1'}]
        topology['links'].append({'node': 'R1', 'interface': 'Gi8', 'network': 'mgmt'})
        apply(self.client, topology)
        self.assertEqual(self.client.ports['1']['7']['network_id'], 1)
        self.assertEqual(self.client.ports['1']['8']['network_id'], 0)
        self.assertEqual(self.client.ports['1']['14']['network_id'], 2)
        self.assertEqual(self.client.networks['1']['visibility'], 0)
        self.assertEqual(apply(self.client, topology)['changes'], [])

    def test_direct_link_validation_and_stable_name(self):
        topology = self.direct_topology()
        del topology["links"][0]["name"]
        first, _ = expand_links(topology)
        link = topology["links"][0]
        link["from"], link["to"] = link["to"], link["from"]
        second, _ = expand_links(topology)
        self.assertEqual(first["networks"], second["networks"])
        topology["links"].append(copy.deepcopy(link))
        with self.assertRaises(ValueError):
            validate(topology)

    def test_delete_stops_all_remote_nodes_and_is_repeatable(self):
        apply(self.client, self.topology)
        self.client.nodes["2"] = {"name": "extra", "status": 2}
        self.client.writes.clear()
        result = delete(self.client, self.topology)
        self.assertTrue(result["deleted"])
        self.assertEqual(result["stopped_nodes"], ["extra"])
        self.assertEqual([entry[1] for entry in self.client.writes],
                         ["labs/palo-lab.unl/nodes/2/stop", "labs/palo-lab.unl"])
        self.assertEqual(self.client.nodes, {})
        self.assertEqual(self.client.networks, {})
        self.client.writes.clear()
        self.assertFalse(delete(self.client, self.topology)["deleted"])
        self.assertEqual(self.client.writes, [])

    def test_delete_aborts_when_stop_fails(self):
        apply(self.client, self.topology)
        self.client.nodes["1"]["status"] = 2
        original = self.client.request
        def request(method, path, payload=None):
            if path.endswith("/stop"):
                raise EveAPIError("stop failed", 500)
            return original(method, path, payload)
        self.client.request = request
        with self.assertRaisesRegex(RuntimeError, "stop failed"):
            delete(self.client, self.topology)
        self.assertTrue(self.client.exists)

    def test_delete_rejects_unsuccessful_verification(self):
        apply(self.client, self.topology)
        original = self.client.request
        def request(method, path, payload=None):
            if method == "DELETE":
                return None
            return original(method, path, payload)
        self.client.request = request
        with self.assertRaisesRegex(RuntimeError, "still reports the lab"):
            delete(self.client, self.topology)

    def test_delete_propagates_permission_failure(self):
        with patch.object(self.client, "request", side_effect=EveAPIError("forbidden", 403)):
            with self.assertRaises(EveAPIError):
                delete(self.client, self.topology)

    def test_missing_image_prevents_all_writes(self):
        self.client.images = {}
        with self.assertRaisesRegex(ValueError, "not available"):
            apply(self.client, self.topology)
        self.assertEqual(self.client.writes, [])

    def test_drift_prevents_all_writes(self):
        apply(self.client, self.topology)
        self.client.nodes["1"]["image"] = "different-image"
        self.client.writes.clear()
        with self.assertRaisesRegex(RuntimeError, "Conflict on R1.image"):
            apply(self.client, self.topology)
        self.assertEqual(self.client.writes, [])

    def test_increase_ethernet_stopped_and_repeat(self):
        apply(self.client, self.topology)
        self.topology['nodes'][0]['ethernet'] = 8
        self.client.writes.clear()
        apply(self.client, self.topology)
        self.assertEqual(self.client.writes, [('PUT', 'labs/palo-lab.unl/nodes/1', {'name': 'R1', 'ethernet': 8})])
        self.assertEqual(apply(self.client, self.topology)['changes'], [])

    def test_shrink_disconnects_removed_ports_before_resize(self):
        apply(self.client, self.topology)
        self.client.ports['1'] = {str(i): {'name': 'Gi' + str(i+1), 'network_id': 1 if i in (0, 3) else 0} for i in range(4)}
        self.topology['nodes'][0]['ethernet'] = 2
        original = self.client.request
        def request(method, path, payload=None):
            if method == 'PUT' and path.endswith('/nodes/1') and 'ethernet' in payload:
                self.assertEqual(self.client.ports['1']['3']['network_id'], 0)
                self.client.ports['1'] = {k:v for k,v in self.client.ports['1'].items() if int(k) < payload['ethernet']}
            return original(method, path, payload)
        self.client.request = request
        apply(self.client, self.topology)
        self.assertEqual(len(self.client.ports['1']), 2)
        self.assertEqual(apply(self.client, self.topology)['changes'], [])

    def test_shrink_rejects_yaml_link_on_removed_port(self):
        apply(self.client, self.topology)
        self.client.ports['1'] = {str(i): {'name': 'Gi' + str(i+1), 'network_id': 0} for i in range(4)}
        self.topology['nodes'][0]['ethernet'] = 2
        self.topology['links'][0]['interface'] = 'Gi4'
        self.client.writes.clear()
        with self.assertRaisesRegex(RuntimeError, 'YAML link uses removed'):
            apply(self.client, self.topology)
        self.assertEqual(self.client.writes, [])

    def test_ethernet_shrink_and_running_growth_refused(self):
        apply(self.client, self.topology)
        self.client.writes.clear()
        self.topology['nodes'][0]['ethernet'] = 2
        with self.assertRaisesRegex(RuntimeError, 'Cannot reduce'):
            apply(self.client, self.topology, prune=False)
        self.topology['nodes'][0]['ethernet'] = 8
        self.client.nodes['1']['status'] = 2
        with self.assertRaisesRegex(RuntimeError, 'Stop R1'):
            apply(self.client, self.topology, prune=False)
        self.assertEqual(self.client.writes, [])

    def test_apply_updates_stopped_resources_and_rerun_is_noop(self):
        apply(self.client, self.topology)
        self.topology["nodes"][0].update(ram=4096, cpu=2)
        self.client.writes.clear()
        apply(self.client, self.topology)
        self.assertEqual(self.client.writes, [("PUT", "labs/palo-lab.unl/nodes/1", {"name": "R1", "cpu": 2, "ram": 4096})])
        self.assertEqual(self.client.nodes["1"]["status"], 0)
        self.client.writes.clear()
        self.assertEqual(apply(self.client, self.topology)["changes"], [])
        self.assertEqual(self.client.writes, [])

    def test_apply_updates_console_and_explicit_positions(self):
        apply(self.client, self.topology)
        self.topology['nodes'][0].update(console='telnet', left=350, top=400)
        self.client.writes.clear()
        apply(self.client, self.topology)
        self.assertEqual(self.client.writes, [('PUT', 'labs/palo-lab.unl/nodes/1',
                         {'name': 'R1', 'console': 'telnet', 'left': 350, 'top': 400})])
        self.client.writes.clear()
        self.assertEqual(apply(self.client, self.topology)['changes'], [])

    def test_apply_updates_network_settings(self):
        apply(self.client, self.topology)
        network = self.topology['networks'][0]
        network.update(type='bridge', left=300, top=350)
        apply(self.client, self.topology)
        actual = next(iter(self.client.networks.values()))
        for key in ('type', 'left', 'top'):
            self.assertEqual(actual[key], network[key])
        self.assertEqual(apply(self.client, self.topology)['changes'], [])

    def test_running_console_change_rejected(self):
        apply(self.client, self.topology)
        self.client.nodes['1']['status'] = 2
        self.topology['nodes'][0]['console'] = 'telnet'
        self.client.writes.clear()
        with self.assertRaisesRegex(RuntimeError, 'Stop R1'):
            apply(self.client, self.topology, prune=False)
        self.assertEqual(self.client.writes, [])

    def test_running_resource_update_rejected_before_writes(self):
        apply(self.client, self.topology)
        self.client.nodes["1"]["status"] = 2
        self.topology["nodes"][0]["ram"] = 4096
        self.client.writes.clear()
        with self.assertRaisesRegex(RuntimeError, "Stop R1"):
            apply(self.client, self.topology, prune=False)
        self.assertEqual(self.client.writes, [])

    def test_resource_update_must_persist(self):
        apply(self.client, self.topology)
        self.topology["nodes"][0]["ram"] = 4096
        original = self.client.request
        def request(method, path, payload=None):
            if method == "PUT" and path.endswith("/nodes/1"):
                return None
            return original(method, path, payload)
        self.client.request = request
        with self.assertRaisesRegex(RuntimeError, "Conflict on R1.ram"):
            apply(self.client, self.topology)

    def test_link_conflict_does_not_rewire(self):
        apply(self.client, self.topology)
        self.client.ports["1"]["7"]["network_id"] = 99
        self.client.writes.clear()
        with self.assertRaisesRegex(RuntimeError, "already connects"):
            apply(self.client, self.topology, prune=False)
        self.assertEqual(self.client.writes, [])

    def test_invalid_interface_reports_partial_creation(self):
        self.topology["links"][0]["interface"] = "Gi9"
        with self.assertRaisesRegex(RuntimeError, "No rollback performed"):
            apply(self.client, self.topology)
        self.assertEqual(len(self.client.nodes), 1)
        self.assertFalse(any(method == "PUT" for method, _, _ in self.client.writes))

    def test_retry_after_partial_failure(self):
        self.client.fail_link = True
        with self.assertRaisesRegex(RuntimeError, "connection failed"):
            apply(self.client, self.topology)
        self.client.fail_link = False
        self.client.writes.clear()
        apply(self.client, self.topology)
        self.assertEqual(len(self.client.writes), 1)
        self.assertEqual(self.client.writes[0][0], "PUT")

    def test_duplicate_remote_names_fail(self):
        apply(self.client, self.topology)
        self.client.nodes["2"] = dict(self.client.nodes["1"])
        self.client.writes.clear()
        with self.assertRaisesRegex(RuntimeError, "Duplicate remote name"):
            apply(self.client, self.topology)
        self.assertEqual(self.client.writes, [])

    def test_start_retries_network_creation_failure(self):
        apply(self.client, self.topology)
        original = self.client.request
        attempts = []
        def request(method, path, payload=None):
            if path.endswith("/start"):
                attempts.append(path)
                if len(attempts) == 1:
                    raise EveAPIError("Failed to create network (11).", 400)
            return original(method, path, payload)
        self.client.request = request
        with patch("eve_lab.deploy.time.sleep"):
            result = lifecycle(self.client, self.topology, "start")
        self.assertEqual(len(attempts), 2)
        self.assertEqual(result["changed_nodes"], ["R1"])

    def test_start_retry_is_bounded_and_specific(self):
        for message, code, count in (("Failed to create network (11).", 400, 3),
                                     ("Unauthorized", 401, 1),
                                     ("Image missing", 400, 1)):
            with self.subTest(message=message):
                client = FakeEve()
                apply(client, self.topology)
                original = client.request
                attempts = []
                def request(method, path, payload=None):
                    if path.endswith("/start"):
                        attempts.append(path)
                        raise EveAPIError(message, code)
                    return original(method, path, payload)
                client.request = request
                with patch("eve_lab.deploy.time.sleep"), self.assertRaises(RuntimeError):
                    lifecycle(client, self.topology, "start")
                self.assertEqual(len(attempts), count)

    def test_start_does_not_retry_if_already_running_after_error(self):
        apply(self.client, self.topology)
        original = self.client.request
        attempts = []
        def request(method, path, payload=None):
            if path.endswith("/start"):
                attempts.append(path)
                self.client.nodes["1"]["status"] = 2
                raise EveAPIError("Failed to create network (11).", 400)
            return original(method, path, payload)
        self.client.request = request
        with patch("eve_lab.deploy.time.sleep"):
            lifecycle(self.client, self.topology, "start")
        self.assertEqual(len(attempts), 1)

    def test_start_checks_status_after_final_network_error(self):
        apply(self.client, self.topology)
        original = self.client.request
        attempts = []
        def request(method, path, payload=None):
            if path.endswith('/start'):
                attempts.append(path)
                if len(attempts) == 3:
                    self.client.nodes['1']['status'] = 2
                raise EveAPIError('Failed to create network (11).', 400)
            return original(method, path, payload)
        self.client.request = request
        with patch('eve_lab.deploy.time.sleep'):
            result = lifecycle(self.client, self.topology, 'start')
        self.assertEqual(len(attempts), 3)
        self.assertEqual(result['changed_nodes'], ['R1'])

    def test_lifecycle_only_declared_nodes_and_skip_repeats(self):
        apply(self.client, self.topology)
        self.client.nodes["2"] = {"name": "unmanaged", "status": 0}
        self.assertEqual(lifecycle(self.client, self.topology, "start")["changed_nodes"], ["R1"])
        self.assertEqual(lifecycle(self.client, self.topology, "start")["changed_nodes"], [])
        self.assertEqual(self.client.nodes["2"]["status"], 0)
        self.assertEqual(lifecycle(self.client, self.topology, "stop")["changed_nodes"], ["R1"])

    def test_stop_uses_remote_ids_despite_invalid_local_nodes(self):
        apply(self.client, self.topology)
        self.client.nodes["1"]["status"] = 2
        self.client.nodes["2"] = {"name": "R1", "status": 2}
        self.topology["nodes"] = [{"name": "not-deployed", "ram": -1}]
        result = lifecycle(self.client, self.topology, "stop")
        self.assertEqual(result["changed_nodes"], ["R1", "R1"])
        self.assertTrue(all(node["status"] == 0 for node in self.client.nodes.values()))
        self.assertEqual(lifecycle(self.client, self.topology, "stop")["changed_nodes"], [])

    def test_stop_selected_remote_node_leaves_others_running(self):
        apply(self.client, self.topology)
        self.client.nodes['1']['status'] = 2
        self.client.nodes['2'] = {'name': 'PA1', 'status': 2}
        self.topology['nodes'] = []
        result = lifecycle(self.client, self.topology, 'stop', node_name='PA1')
        self.assertEqual(result['changed_nodes'], ['PA1'])
        self.assertEqual(self.client.nodes['1']['status'], 2)
        self.assertEqual(self.client.nodes['2']['status'], 0)
        self.assertEqual(lifecycle(self.client, self.topology, 'stop', node_name='PA1')['changed_nodes'], [])

    def test_start_selected_remote_node_leaves_others_stopped(self):
        apply(self.client, self.topology)
        self.client.nodes['1']['status'] = 0
        self.client.nodes['2'] = {'name': 'PA1', 'status': 0}
        self.topology['nodes'] = [{'name': 'not-deployed'}]
        result = lifecycle(self.client, self.topology, 'start', node_name='PA1')
        self.assertEqual(result['changed_nodes'], ['PA1'])
        self.assertEqual(self.client.nodes['1']['status'], 0)
        self.assertEqual(self.client.nodes['2']['status'], 2)
        self.assertEqual(lifecycle(self.client, self.topology, 'start', node_name='PA1')['changed_nodes'], [])

    def test_start_missing_selected_node_does_not_start_others(self):
        apply(self.client, self.topology)
        self.client.nodes['1']['status'] = 0
        with self.assertRaisesRegex(RuntimeError, 'Missing nodes'):
            lifecycle(self.client, self.topology, 'start', node_name='missing')
        self.assertEqual(self.client.nodes['1']['status'], 0)

    def test_stop_selection_rejects_missing_or_duplicate_names(self):
        apply(self.client, self.topology)
        self.client.nodes['1']['status'] = 2
        with self.assertRaisesRegex(ValueError, 'found 0'):
            lifecycle(self.client, self.topology, 'stop', node_name='missing')
        self.client.nodes['2'] = {'name': 'R1', 'status': 2}
        with self.assertRaisesRegex(ValueError, 'found 2'):
            lifecycle(self.client, self.topology, 'stop', node_name='R1')
        self.assertTrue(all(n['status'] == 2 for n in self.client.nodes.values()))

    @patch("eve_lab.deploy.STOP_TIMEOUT", 0)
    def test_stop_continues_after_one_node_fails(self):
        apply(self.client, self.topology)
        self.client.nodes["1"]["status"] = 2
        self.client.nodes["2"] = {"name": "PA1", "status": 2}
        original = self.client.request
        def request(method, path, payload=None):
            if path.endswith("/nodes/1/stop"):
                raise EveAPIError("stop failed", 500)
            return original(method, path, payload)
        self.client.request = request
        with self.assertRaisesRegex(RuntimeError, "Completed nodes:.*PA1"):
            lifecycle(self.client, self.topology, "stop")
        self.assertEqual(self.client.nodes["2"]["status"], 0)

    @patch("eve_lab.deploy.STOP_TIMEOUT", 0)
    def test_stop_detects_server_did_not_stop_node(self):
        apply(self.client, self.topology)
        self.client.nodes["1"]["status"] = 2
        original = self.client.request
        def request(method, path, payload=None):
            if path.endswith("/stop"):
                return None
            return original(method, path, payload)
        self.client.request = request
        with self.assertRaisesRegex(RuntimeError, "still active") as caught:
            lifecycle(self.client, self.topology, "stop")
        self.assertIn("Completed nodes: []", str(caught.exception))
        self.assertIn("Stop requests accepted: ['R1']", str(caught.exception))

    def test_stop_and_delete_wait_for_async_shutdown(self):
        for operation in ("stop", "delete"):
            with self.subTest(operation=operation):
                client = FakeEve()
                apply(client, self.topology)
                client.nodes["1"]["status"] = 2
                original = client.request
                def request(method, path, payload=None):
                    if path.endswith("/stop"):
                        return None  # Accepted; VM remains active until the next poll.
                    return original(method, path, payload)
                client.request = request
                def finish_shutdown(_):
                    client.nodes["1"]["status"] = 0
                with patch("eve_lab.deploy.time.sleep", side_effect=finish_shutdown) as sleep:
                    result = (delete(client, self.topology) if operation == "delete"
                              else lifecycle(client, self.topology, "stop"))
                sleep.assert_called_once()
                if operation == "stop":
                    self.assertEqual(result["changed_nodes"], ["R1"])
                else:
                    self.assertTrue(result["deleted"])

    def test_stop_target_ignores_topology_edits(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            lab = root / "labs" / "palo-lab"
            lab.mkdir(parents=True)
            (lab / "topology.yaml").write_text("name: renamed\nremote_folder: /My Labs\nnodes: invalid\nlinks: invalid\n")
            self.assertEqual(load_lab_target(root, "palo-lab"),
                             {"name": "palo-lab", "remote_folder": "/My Labs"})
            (lab / "topology.yaml").write_text("nodes: [")
            self.assertEqual(load_lab_target(root, "palo-lab", "/")["remote_folder"], "/")
            with self.assertRaisesRegex(ValueError, "--remote-folder"):
                load_lab_target(root, "palo-lab")
            self.assertEqual(load_lab_target(root, "missing-lab", "/")["name"], "missing-lab")

    def test_running_node_cannot_be_connected(self):
        apply(self.client, self.topology)
        self.client.nodes["1"]["status"] = 2
        self.client.ports["1"]["7"]["network_id"] = 0
        self.client.writes.clear()
        with self.assertRaisesRegex(RuntimeError, "Stop R1"):
            apply(self.client, self.topology, prune=False)
        self.assertEqual(self.client.writes, [])

    def test_list_interface_response(self):
        apply(self.client, self.topology)
        self.client.list_ports = True
        self.assertEqual(apply(self.client, self.topology)["changes"], [])

    def test_non_404_failure_never_creates_lab(self):
        original = self.client.request
        def request(method, path, payload=None):
            if path == "labs/palo-lab.unl":
                raise EveAPIError("forbidden", 403)
            return original(method, path, payload)
        self.client.request = request
        with self.assertRaises(EveAPIError):
            apply(self.client, self.topology)
        self.assertEqual(self.client.writes, [])

    def test_invalid_references_and_resources(self):
        for field, value in (("cpu", 0), ("ethernet", True), ("ram", "4096")):
            with self.subTest(field=field):
                topology = copy.deepcopy(self.topology)
                topology["nodes"][0][field] = value
                with self.assertRaises(ValueError):
                    validate(topology)
        self.topology["links"][0]["network"] = "missing"
        with self.assertRaisesRegex(ValueError, "Unknown node or network"):
            validate(self.topology)

    def test_duplicate_alias_links(self):
        self.topology["links"].append({"node": "R1", "interface": "g1", "network": "mgmt"})
        with self.assertRaisesRegex(ValueError, "more than once"):
            validate(self.topology)

    def test_encoded_folder(self):
        self.topology["remote_folder"] = "/My Labs/"
        self.assertEqual(lab_path(self.topology), "labs/My%20Labs/palo-lab.unl")

    def test_jsend_failure_preserves_error_code(self):
        client = EveClient("http://example.invalid")
        with patch.object(client.opener, "open"), patch("eve_lab.client.json.load", return_value={
            "status": "fail", "code": "404", "message": "missing"
        }):
            with self.assertRaises(EveAPIError) as caught:
                client.request("GET", "labs/missing.unl")
        self.assertEqual(caught.exception.code, 404)

    def test_http_error_preserves_server_message(self):
        client = EveClient("http://example.invalid")
        error = HTTPError("http://example.invalid", 400, "Bad Request", {}, io.BytesIO(
            b'{"status":"fail","message":"Failed to lock the lab (60061)."}'
        ))
        with patch.object(client.opener, "open", side_effect=error):
            with self.assertRaisesRegex(EveAPIError, "Failed to lock the lab") as caught:
                client.request("POST", "labs/palo-lab.unl/nodes", {})
        self.assertEqual(caught.exception.code, 400)

    def test_http_error_non_json_body_keeps_status(self):
        client = EveClient("http://example.invalid")
        error = HTTPError("http://example.invalid", 500, "Error", {}, io.BytesIO(b"<html>Error</html>"))
        with patch.object(client.opener, "open", side_effect=error):
            with self.assertRaisesRegex(EveAPIError, "HTTP 500"):
                client.request("POST", "labs/palo-lab.unl/nodes", {})

    def test_timeout_reports_uncertain_outcome(self):
        client = EveClient("http://example.invalid", timeout=120)
        with patch.object(client.opener, "open", side_effect=TimeoutError):
            with self.assertRaisesRegex(RuntimeError, "Timed out after 120s during POST"):
                client.request("POST", "labs/palo-lab.unl/nodes", {})


if __name__ == "__main__":
    unittest.main()
