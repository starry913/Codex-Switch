import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from codex_switch.bridge import Bridge


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


if __name__ == "__main__":
    unittest.main()
