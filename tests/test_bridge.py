import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from codex_switch.bridge import Bridge, distributions, run


class BridgeTests(unittest.TestCase):
    def test_full_rpc_round_trip_does_not_expose_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "中文 path"
            home.mkdir()
            (home / "config.toml").write_text('model = "test"\n')
            (home / "auth.json").write_text('{"OPENAI_API_KEY":"private-placeholder"}')
            bridge = Bridge(home=str(home))
            info = bridge.call("diagnostic")
            self.assertEqual(info["model"], "test")
            self.assertNotIn("private-placeholder", json.dumps(info))
            bridge.call("capture", name="原连接", password="test-password-long")
            result = bridge.call("profiles", password="test-password-long")
            self.assertEqual(result["profiles"][0]["name"], "原连接")
            self.assertNotIn("private-placeholder", json.dumps(result))

    def test_rpc_rejects_arbitrary_methods(self):
        p = subprocess.run([sys.executable, "-m", "codex_switch", "--rpc"], input=b'{"action":"write_snapshot"}', capture_output=True)
        result = json.loads(p.stdout)
        self.assertFalse(result["ok"])

    def test_frozen_windows_uses_in_process_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "codex"
            home.mkdir()
            (home / "config.toml").write_text('model = "packaged"\n')
            with mock.patch.object(sys, "frozen", True, create=True):
                bridge = Bridge(home=str(home))
                self.assertTrue(bridge.frozen)
                info = bridge.call("diagnostic")
            self.assertEqual(info["model"], "packaged")

    @unittest.skipUnless(os.name == "nt", "Windows-only process behavior")
    def test_windows_subprocess_is_created_without_a_console(self):
        with mock.patch("codex_switch.bridge.subprocess.run") as mocked_run:
            run(["example.exe"])
        kwargs = mocked_run.call_args.kwargs
        self.assertTrue(kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW)
        self.assertTrue(kwargs["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW)
        self.assertEqual(kwargs["startupinfo"].wShowWindow, subprocess.SW_HIDE)

    @unittest.skipUnless(os.name == "nt", "Windows-only WSL discovery")
    def test_wsl_discovery_prefers_registry_without_spawning_wsl(self):
        fake_root = mock.MagicMock()
        fake_distro = mock.MagicMock()
        fake_root.__enter__.return_value = fake_root
        fake_distro.__enter__.return_value = fake_distro
        with mock.patch("codex_switch.bridge.winreg.OpenKey", side_effect=[fake_root, fake_distro]), \
                mock.patch("codex_switch.bridge.winreg.QueryInfoKey", return_value=(1, 0, 0)), \
                mock.patch("codex_switch.bridge.winreg.EnumKey", return_value="test-id"), \
                mock.patch("codex_switch.bridge.winreg.QueryValueEx", return_value=("Ubuntu-22.04", 1)), \
                mock.patch("codex_switch.bridge.run") as mocked_run:
            self.assertEqual(distributions(), ["Ubuntu-22.04"])
        mocked_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
