from pathlib import Path
import tempfile
import time
import unittest

from starlette.testclient import TestClient

from room_service.remote import RemoteSettings, create_remote_app, session_cookie

ORIGIN = "https://room.example.test"
TOKEN = "mcp-" + "a" * 48
PASSWORD = "web-" + "b" * 48


class RemoteRoomTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = RemoteSettings(TOKEN, PASSWORD, ORIGIN, Path(self.temp.name) / "room.db")
        self.client = TestClient(create_remote_app(self.settings), base_url=ORIGIN)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def login(self):
        result = self.client.post("/login", data={"password": PASSWORD}, headers={"Origin": ORIGIN}, follow_redirects=False)
        self.assertEqual(303, result.status_code)
        self.assertIn("Secure", result.headers["set-cookie"])
        self.assertIn("HttpOnly", result.headers["set-cookie"])
        self.assertIn("SameSite=strict", result.headers["set-cookie"])

    def mcp(self, method, params=None):
        return self.client.post("/mcp", headers={"Authorization": "Bearer " + TOKEN, "Accept": "application/json, text/event-stream"},
                                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})

    def test_unauthenticated_site_redirects_and_api_mcp_are_denied(self):
        self.assertEqual("/login", self.client.get("/", follow_redirects=False).headers["location"])
        for url in ["/api/v1/room/state", "/health", "/mcp"]:
            self.assertEqual(401, self.client.get(url).status_code)
        self.assertEqual(200, self.client.get("/login").status_code)

    def test_browser_login_rest_and_logout_without_leaking_mcp_token(self):
        self.login()
        self.assertEqual(200, self.client.get("/").status_code)
        state = self.client.get("/api/v1/room/state")
        self.assertEqual(200, state.status_code)
        self.assertNotIn(TOKEN, state.text)
        stopped = self.client.post("/api/v1/room/characters/owner/stop", headers={"Origin": ORIGIN})
        self.assertEqual(200, stopped.status_code)
        self.assertEqual(401, self.client.get("/health").status_code)
        self.assertEqual(401, self.client.post("/mcp", json={}).status_code)
        self.client.post("/logout", headers={"Origin": ORIGIN})
        self.assertEqual(401, self.client.get("/api/v1/room/state").status_code)

    def test_bearer_cannot_write_owner_and_cookie_cannot_write_companion(self):
        self.assertEqual(401, self.client.post("/api/v1/room/characters/owner/stop", headers={"Origin": ORIGIN, "Authorization": "Bearer " + TOKEN}).status_code)
        self.login()
        self.assertEqual(404, self.client.post("/api/v1/room/characters/companion/stop", headers={"Origin": ORIGIN}).status_code)
        self.assertEqual(404, self.client.get("/api/v1/room/health").status_code)

    def test_csrf_invalid_host_and_expired_cookie_are_rejected(self):
        self.login()
        self.assertEqual(403, self.client.post("/api/v1/room/characters/owner/stop", headers={"Origin": "https://evil.example"}).status_code)
        self.assertEqual(403, self.client.post("/api/v1/room/characters/owner/stop").status_code)
        self.assertEqual(400, self.client.get("/", headers={"Host": "evil.example"}).status_code)
        self.client.cookies.clear()
        self.client.cookies.set("room_session", session_cookie(PASSWORD, int(time.time()) - 1))
        self.assertEqual(401, self.client.get("/api/v1/room/state").status_code)

    def test_invalid_login_is_throttled_and_oversized_body_rejected(self):
        for _ in range(10):
            self.assertEqual(401, self.client.post("/login", data={"password": "wrong"}, headers={"Origin": ORIGIN}).status_code)
        self.assertEqual(429, self.client.post("/login", data={"password": PASSWORD}, headers={"Origin": ORIGIN}).status_code)
        self.assertEqual(413, self.client.post("/login", content=b"a" * 5000, headers={"Origin": ORIGIN}).status_code)

    def test_real_http_mcp_initialize_tools_and_rest_shared_state(self):
        result = self.mcp("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "room-test", "version": "1"}})
        self.assertEqual(200, result.status_code, result.text)
        tools = self.mcp("tools/list").json()["result"]["tools"]
        self.assertEqual({"room_get_state", "room_move", "room_use_furniture", "room_stop"}, {tool["name"] for tool in tools})
        result = self.mcp("tools/call", {"name": "room_get_state", "arguments": {}})
        self.assertEqual(200, result.status_code, result.text)
        self.assertFalse(result.json()["result"].get("isError", False))
        self.login()
        import json
        mcp_state = json.loads(result.json()["result"]["content"][0]["text"])
        rest_state = self.client.get("/api/v1/room/state").json()
        self.assertEqual(rest_state["revision"], mcp_state["revision"])
        self.assertEqual({"owner", "companion"}, {item["id"] for item in mcp_state["characters"]})
        used = self.mcp("tools/call", {"name": "room_use_furniture", "arguments": {"furniture_id": "sofa", "interaction": "sit"}})
        self.assertFalse(used.json()["result"].get("isError", False), used.text)
        state = self.client.get("/api/v1/room/state").json()
        characters = {item["id"]: item for item in state["characters"]}
        self.assertEqual("idle", characters["owner"]["activity"])
        self.assertIsNotNone(characters["companion"].get("movement"))
        stopped = self.mcp("tools/call", {"name": "room_stop", "arguments": {}})
        self.assertFalse(stopped.json()["result"].get("isError", False))
        self.assertEqual("idle", next(item for item in self.client.get("/api/v1/room/state").json()["characters"] if item["id"] == "companion")["activity"])

    def test_restart_preserves_shared_database(self):
        self.mcp("tools/call", {"name": "room_use_furniture", "arguments": {"furniture_id": "sofa", "interaction": "sit"}})
        with TestClient(create_remote_app(self.settings), base_url=ORIGIN) as restarted:
            import json
            state = restarted.post("/mcp", headers={"Authorization": "Bearer " + TOKEN, "Accept": "application/json, text/event-stream"},
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "room_get_state", "arguments": {}}}).json()
            companion = next(item for item in json.loads(state["result"]["content"][0]["text"])["characters"] if item["id"] == "companion")
            self.assertEqual("sofa", companion["furniture"])

    def test_missing_weak_or_equal_credentials_are_rejected(self):
        for token, password in [("", PASSWORD), (TOKEN, "short"), (TOKEN, TOKEN)]:
            with self.assertRaises(ValueError):
                create_remote_app(RemoteSettings(token, password, ORIGIN, self.settings.db_path))


if __name__ == "__main__":
    unittest.main()
