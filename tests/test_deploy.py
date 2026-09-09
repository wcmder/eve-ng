import copy
import io
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from eve_lab.client import EveAPIError, EveClient
from eve_lab.deploy import apply, delete, lab_path, lifecycle
from eve_lab.topology import load_topology, validate


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
                ident = str(len(objects) + 1)
                objects[ident] = {**payload, "id": int(ident)}
                if kind == "nodes":
                    objects[ident]["status"] = 0
                    # Deliberately nonzero/noncontiguous ID: never guess from Gi1.
                    self.ports[ident] = {"7": {"name": "Gi1", "network_id": 0}}
                return None
        if "/nodes/" in path:
            ident, action = path.split("/nodes/")[1].split("/")
            if action == "interfaces":
                if method == "GET":
                    ports = copy.deepcopy(self.ports[ident])
                    return {"ethernet": list(ports.values()) if self.list_ports else ports}
                if self.fail_link:
                    raise EveAPIError("connection failed", 500)
                for port, target in payload.items():
                    self.ports[ident][port]["network_id"] = int(target)
                return None
            if action in ("start", "stop"):
                self.nodes[ident]["status"] = 2 if action == "start" else 0
                return None
        raise AssertionError((method, path, payload))


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.topology = load_topology(Path(__file__).resolve().parents[1], "palo-lab")
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
        self.client.nodes["1"]["cpu"] = 2
        self.client.writes.clear()
        with self.assertRaisesRegex(RuntimeError, "Conflict on R1.cpu"):
            apply(self.client, self.topology)
        self.assertEqual(self.client.writes, [])

    def test_link_conflict_does_not_rewire(self):
        apply(self.client, self.topology)
        self.client.ports["1"]["7"]["network_id"] = 99
        self.client.writes.clear()
        with self.assertRaisesRegex(RuntimeError, "already connects"):
            apply(self.client, self.topology)
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

    def test_lifecycle_only_declared_nodes_and_skip_repeats(self):
        apply(self.client, self.topology)
        self.client.nodes["2"] = {"name": "unmanaged", "status": 0}
        self.assertEqual(lifecycle(self.client, self.topology, "start")["changed_nodes"], ["R1"])
        self.assertEqual(lifecycle(self.client, self.topology, "start")["changed_nodes"], [])
        self.assertEqual(self.client.nodes["2"]["status"], 0)
        self.assertEqual(lifecycle(self.client, self.topology, "stop")["changed_nodes"], ["R1"])

    def test_running_node_cannot_be_connected(self):
        apply(self.client, self.topology)
        self.client.nodes["1"]["status"] = 2
        self.client.ports["1"]["7"]["network_id"] = 0
        self.client.writes.clear()
        with self.assertRaisesRegex(RuntimeError, "Stop R1"):
            apply(self.client, self.topology)
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
