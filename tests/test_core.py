import base64
import json
import os
import stat
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import tomlkit

from codex_switch.core import Manager, SwitchError, Vault, atomic_write, digest, is_switch_client, validate_url

PASSWORD = "test-password-not-real"
CONFIG = b'''# Personal settings must survive switching.
model_provider = "old"
model = "original-model"
model_reasoning_effort = "high"

[model_providers.old]
name = "Existing relay"
base_url = "http://127.0.0.1:8765/v1"
requires_openai_auth = true
wire_api = "responses"
request_max_retries = 4

[projects."C:\\\\research"]
trust_level = "trusted"

[mcp_servers.local]
command = "my-local-tool"

[features]
some_future_feature = true
'''


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / "codex"
        self.home.mkdir()
        (self.home / "config.toml").write_bytes(CONFIG)
        (self.home / "auth.json").write_text('{"OPENAI_API_KEY":"original-test-secret"}')
        for name in ("sessions/old.jsonl", "memories/project.md", "skills/mine/SKILL.md", "history.jsonl", ".codex-global-state.json"):
            p = self.home / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("protected-work-state")
        self.manager = Manager(self.home, process_check=lambda: [])
        self.before = self.manager.files()
        self.protected = self.protected_files()

    def tearDown(self):
        self.temp.cleanup()

    def protected_files(self):
        return {str(p.relative_to(self.home)): p.read_bytes() for p in self.home.rglob("*")
                if p.is_file() and p.name not in ("config.toml", "auth.json") and "switch-local" not in p.parts}

    def seed(self):
        self.manager.capture("original", PASSWORD)
        self.manager.add_api("PackyCode", "https://example.test/v1", "new-test-secret", "compatible-model", PASSWORD)

    def test_round_trip_preserves_all_non_connection_state(self):
        self.seed()
        result = self.manager.apply("PackyCode", PASSWORD)
        self.assertFalse(result["continuation_verified"])
        after = tomlkit.parse((self.home / "config.toml").read_text())
        before = tomlkit.parse(CONFIG.decode())
        for k in ("projects", "mcp_servers", "features", "model_reasoning_effort"):
            self.assertEqual(before[k], after[k])
        self.assertIn("# Personal settings", (self.home / "config.toml").read_text())
        self.assertEqual(self.protected, self.protected_files())
        self.manager.restore(PASSWORD)
        self.assertEqual(self.before, self.manager.files())
        self.assertEqual(self.protected, self.protected_files())

    def test_snapshot_vault_contains_no_plaintext_secrets(self):
        self.seed()
        self.manager.apply("PackyCode", PASSWORD)
        raw = (self.manager.store / "profiles.vault").read_bytes()
        for secret in (b"original-test-secret", b"new-test-secret", b"PackyCode", CONFIG):
            self.assertNotIn(secret, raw)
        with self.assertRaises(SwitchError):
            self.manager.profiles("wrong-password-long")

    def test_process_guard_blocks_switch_and_restore(self):
        self.seed()
        self.manager.process_check = lambda: [{"name": "codex", "pid": 123}]
        with self.assertRaises(SwitchError):
            self.manager.apply("PackyCode", PASSWORD)
        self.assertEqual(self.before, self.manager.files())

    def test_preview_is_read_only(self):
        self.seed()
        vault = (self.manager.store / "profiles.vault").read_bytes()
        self.assertEqual(set(self.manager.preview("PackyCode", PASSWORD)["changed_files"]), {"config.toml", "auth.json"})
        self.assertEqual(vault, (self.manager.store / "profiles.vault").read_bytes())
        self.assertEqual(self.before, self.manager.files())

    def test_restore_refuses_external_edits(self):
        self.seed()
        self.manager.apply("PackyCode", PASSWORD)
        (self.home / "auth.json").write_text('{"OPENAI_API_KEY":"new-external-login"}')
        changed = self.manager.files()
        with self.assertRaises(SwitchError):
            self.manager.restore(PASSWORD)
        self.assertEqual(changed, self.manager.files())

    def test_partial_write_failure_rolls_back(self):
        self.seed()
        failed = False
        def write(path, data):
            nonlocal failed
            if path == self.home / "auth.json" and not failed:
                failed = True
                raise OSError("simulated failure")
            return atomic_write(path, data)
        with patch("codex_switch.core.atomic_write", side_effect=write):
            with self.assertRaises(SwitchError):
                self.manager.apply("PackyCode", PASSWORD)
        self.assertEqual(self.before, self.manager.files())
        self.assertFalse(Vault(self.manager.store, PASSWORD).data["pending"])

    def test_interrupted_transaction_recovery(self):
        self.seed()
        self.manager.apply("PackyCode", PASSWORD)
        v = Vault(self.manager.store, PASSWORD)
        v.data["pending"] = v.data["backups"][-1]["id"]
        v.save()
        # Mimic a crash with only the first file replaced.
        (self.home / "auth.json").write_bytes(self.before["auth.json"])
        with self.assertRaises(SwitchError):
            self.manager.apply("original", PASSWORD)
        self.manager.restore(PASSWORD)
        self.assertEqual(self.before, self.manager.files())

    def test_concurrent_edit_before_write_is_not_overwritten(self):
        self.seed()
        original_save = Vault.save
        def save(v):
            original_save(v)
            if v.data["pending"]:
                (self.home / "config.toml").write_bytes(CONFIG + b"\n# external edit\n")
        with patch.object(Vault, "save", save):
            with self.assertRaises(SwitchError):
                self.manager.apply("PackyCode", PASSWORD)
        self.assertIn(b"# external edit", (self.home / "config.toml").read_bytes())
        self.assertEqual(self.before["auth.json"], (self.home / "auth.json").read_bytes())

    def test_keyring_not_silently_overridden(self):
        self.seed()
        (self.home / "config.toml").write_bytes(b'cli_auth_credentials_store = "keyring"\n' + CONFIG)
        with self.assertRaises(SwitchError):
            self.manager.apply("PackyCode", PASSWORD)
        with self.assertRaises(SwitchError):
            self.manager.capture("keyring", PASSWORD)

    def test_official_token_refresh_retained_on_switch_away(self):
        (self.home / "config.toml").write_text('model = "official-model"\n')
        (self.home / "auth.json").write_text('{"tokens":{"access_token":"old","refresh_token":"old-refresh"}}')
        self.manager.capture("official", PASSWORD)
        self.manager.add_api("relay", "https://example.test/v1", "key", "model", PASSWORD)
        refreshed = b'{"tokens":{"access_token":"fresh","refresh_token":"fresh-refresh"}}'
        (self.home / "auth.json").write_bytes(refreshed)
        self.manager.apply("relay", PASSWORD)
        self.manager.apply("official", PASSWORD)
        self.assertEqual(refreshed, (self.home / "auth.json").read_bytes())
        c = tomlkit.parse((self.home / "config.toml").read_text())
        self.assertEqual(c.get("model_provider", "openai"), "openai")
        self.assertNotIn("openai_base_url", c)

    def test_profiles_do_not_mix_credentials_and_endpoints(self):
        self.seed()
        self.manager.add_api("RightCode", "https://second.test/v1", "second-key", "second-model", PASSWORD)
        self.manager.apply("PackyCode", PASSWORD)
        first_id = tomlkit.parse((self.home / "config.toml").read_text())["model_provider"]
        self.manager.apply("RightCode", PASSWORD)
        c = tomlkit.parse((self.home / "config.toml").read_text())
        self.assertEqual(first_id, c["model_provider"])
        self.assertEqual(c["model_providers"][first_id]["base_url"], "https://second.test/v1")
        self.assertEqual(json.loads((self.home / "auth.json").read_text())["OPENAI_API_KEY"], "second-key")
        self.manager.apply("original", PASSWORD)
        self.assertEqual(json.loads((self.home / "auth.json").read_text())["OPENAI_API_KEY"], "original-test-secret")
        self.assertEqual(self.protected, self.protected_files())

    def test_read_only_history_includes_multiple_providers(self):
        path = self.home / "state_5.sqlite"
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE threads (id TEXT, title TEXT, cwd TEXT, model_provider TEXT, updated_at INTEGER)")
            db.executemany("INSERT INTO threads VALUES (?,?,?,?,?)", [("1", "Alpha", "C:/a", "old", 1), ("2", "Beta", "/home/b", "new", 2)])
        db.close()
        old = path.read_bytes()
        rows = self.manager.history()
        self.assertEqual({r["provider"] for r in rows}, {"old", "new"})
        self.assertEqual(path.read_bytes(), old)

    def test_environments_are_independent(self):
        self.seed()
        other = Manager(Path(self.temp.name) / "wsl", process_check=lambda: [])
        other.add_api("Other", "https://different.test/v1", "different", "model", PASSWORD)
        self.assertEqual(len(other.profiles(PASSWORD)["profiles"]), 1)
        self.assertEqual(len(self.manager.profiles(PASSWORD)["profiles"]), 2)
        self.assertEqual(self.before, self.manager.files())

    def test_url_and_name_validation(self):
        for url in ("http://remote.test/v1", "https://user:key@example.test", "https://example.test?key=secret", "file:///tmp/a", "https://example.test/#secret"):
            with self.assertRaises(SwitchError):
                validate_url(url)
        for url in ("https://example.test/v1", "http://127.0.0.1:8765/v1"):
            validate_url(url)
        self.seed()
        with self.assertRaises(SwitchError):
            self.manager.capture("original", PASSWORD)

    def test_lock_prevents_concurrent_writers(self):
        with self.manager.locked():
            with self.assertRaises(SwitchError):
                with self.manager.locked():
                    self.fail("second lock acquired")


class QuickSwitchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / "codex"
        self.home.mkdir()
        (self.home / "config.toml").write_text(
            'model = "gpt-5.6-sol"\nmodel_provider = "packycode"\n\n'
            '[projects."C:\\\\research"]\ntrust_level = "trusted"\n',
            encoding="utf-8")
        (self.home / "auth.json").write_text('{"OPENAI_API_KEY":"packy-live-key"}', encoding="utf-8")
        memory = self.home / "memories" / "project.md"
        memory.parent.mkdir()
        memory.write_text("keep-me", encoding="utf-8")
        self.manager = Manager(self.home, process_check=lambda: [])
        self.manager.store.mkdir()
        (self.manager.store / "quick_keys.json").write_text(
            '{"packy":"packy-stale-key","rightcode":"right-stale-key"}', encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_switch_keeps_project_memory_and_uses_saved_key(self):
        self.manager.quick_switch("RightCode")
        config = tomlkit.parse((self.home / "config.toml").read_text(encoding="utf-8"))
        self.assertEqual(config["model_provider"], "rightcode")
        self.assertEqual(config["model"], "gpt-6-astra")
        self.assertEqual(config["projects"]["C:\\research"]["trust_level"], "trusted")
        self.assertEqual(json.loads((self.home / "auth.json").read_text())["OPENAI_API_KEY"], "right-stale-key")
        self.assertEqual((self.home / "memories" / "project.md").read_text(encoding="utf-8"), "keep-me")

    def test_live_key_is_restored_instead_of_stale_file(self):
        (self.home / "config.toml").write_text(
            'model = "gpt-6-astra"\nmodel_provider = "rightcode"\n', encoding="utf-8")
        (self.home / "auth.json").write_text('{"OPENAI_API_KEY":"right-live-key"}', encoding="utf-8")
        self.manager.quick_switch("Packy")
        self.manager.quick_switch("RightCode")
        self.assertEqual(json.loads((self.home / "auth.json").read_text())["OPENAI_API_KEY"], "right-live-key")
        saved = json.loads((self.manager.store / "quick_keys.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["rightcode"], "right-live-key")
        self.assertEqual(saved["packy"], "packy-stale-key")

    def test_official_switch_restores_login_and_drops_relay_key(self):
        (self.home / "config.toml").write_text('model = "gpt-6-astra"\n', encoding="utf-8")
        (self.home / "auth.json").write_text(
            '{"tokens":{"access_token":"official-access","refresh_token":"official-refresh"}}', encoding="utf-8")
        self.manager.quick_switch("Packy")
        self.assertEqual(json.loads((self.home / "auth.json").read_text())["OPENAI_API_KEY"], "packy-stale-key")
        self.manager.quick_switch("官方直连")
        auth = json.loads((self.home / "auth.json").read_text(encoding="utf-8"))
        config = tomlkit.parse((self.home / "config.toml").read_text(encoding="utf-8"))
        self.assertEqual(auth["tokens"]["refresh_token"], "official-refresh")
        self.assertNotIn("OPENAI_API_KEY", auth)
        self.assertNotIn("model_provider", config)
        self.assertNotIn("preferred_auth_method", config)

    def test_old_threads_follow_the_selected_service(self):
        db_path = self.home / "state_5.sqlite"
        rollout = self.home / "sessions" / "old.jsonl"
        rollout.parent.mkdir()
        body = '{"type":"event","payload":{"text":"keep-history"}}\n'
        rollout.write_text(
            '{"type":"session_meta","payload":{"id":"t1","model_provider":"packycode","cwd":"C:/research"}}\n' + body,
            encoding="utf-8")
        db = sqlite3.connect(db_path)
        try:
            db.execute("CREATE TABLE threads (id TEXT, model_provider TEXT, model TEXT, rollout_path TEXT, title TEXT)")
            db.execute("INSERT INTO threads VALUES (?,?,?,?,?)", ("t1", "packycode", "gpt-5.6-sol", str(rollout), "hello"))
            db.execute("INSERT INTO threads VALUES (?,?,?,?,?)", ("review", "packycode", "codex-auto-review", None, "review"))
            db.commit()
        finally:
            db.close()
        self.manager.quick_switch("RightCode")
        db = sqlite3.connect(db_path)
        try:
            rows = {row[0]: (row[1], row[2]) for row in db.execute("SELECT id, model_provider, model FROM threads")}
        finally:
            db.close()
        self.assertEqual(rows["t1"], ("rightcode", "gpt-6-astra"))
        self.assertEqual(rows["review"], ("rightcode", "codex-auto-review"))
        lines = rollout.read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(lines[0])["payload"]["model_provider"], "rightcode")
        self.assertEqual(json.loads(lines[0])["payload"]["cwd"], "C:/research")
        self.assertIn("keep-history", lines[1])
        self.assertEqual((self.home / "memories" / "project.md").read_text(encoding="utf-8"), "keep-me")

    def test_readonly_key_file_can_be_replaced(self):
        key_file = self.manager.store / "quick_keys.json"
        os.chmod(key_file, stat.S_IREAD)
        self.assertFalse(os.access(key_file, os.W_OK))
        self.manager.quick_switch("RightCode")
        self.assertEqual(json.loads(key_file.read_text(encoding="utf-8"))["packy"], "packy-live-key")
        self.assertTrue(os.access(key_file, os.W_OK))

    def test_wsl_cursor_codex_counts_as_a_client(self):
        remote = {"name": "codex", "exe": "/home/lqy/.cursor-server/extensions/openai.chatgpt/bin/codex"}
        editor = {"name": "codex.exe", "exe": r"C:\Users\LQY\.cursor\extensions\openai.chatgpt\bin\codex.exe"}
        self.assertTrue(is_switch_client(remote))
        self.assertFalse(is_switch_client(editor))

    def test_open_desktop_client_blocks_the_write(self):
        self.manager.process_check = lambda: [{"name": "ChatGPT.exe", "pid": 999999, "exe": ""}]
        before = self.manager.files()
        with self.assertRaises(SwitchError):
            self.manager.quick_switch("RightCode", restart=True)
        self.assertEqual(before, self.manager.files())


if __name__ == "__main__":
    unittest.main()
