"""Local subprocess bridge. Secrets travel over stdin, never argv or a network."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from .core import SwitchError


def run(command, **kwargs):
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(command, **kwargs)


def distributions():
    if os.name != "nt":
        return []
    try:
        p = run(["wsl.exe", "--list", "--quiet"], capture_output=True, timeout=10)
        if p.returncode:
            return []
        names = p.stdout.decode("utf-16-le" if b"\x00" in p.stdout else "utf-8", errors="replace")
        return [n.strip() for n in names.splitlines() if n.strip() and not n.strip().startswith("docker-desktop")]
    except (OSError, subprocess.TimeoutExpired):
        return []


class Bridge:
    def __init__(self, distro=None, home=None, store=None):
        self.distro, self.home, self.store = distro, home, store
        self.root = Path(__file__).resolve().parent.parent

    def call(self, action, **args):
        request = json.dumps({"action": action, "home": self.home, "store": self.store, "args": args}, ensure_ascii=False)
        if self.distro:
            # `wsl -- command` waits on this machine and never returns. `-e` executes directly.
            mapped = run(["wsl.exe", "-d", self.distro, "-e", "wslpath", "-u", str(self.root)], capture_output=True, timeout=15)
            if mapped.returncode:
                raise SwitchError("无法访问 WSL 内的工具目录。请检查发行版是否可启动。")
            root = mapped.stdout.decode("utf-8").strip()
            # Positional arguments avoid interpolating user paths into shell code.
            command = ["wsl.exe", "-d", self.distro, "-e", "sh", "-c",
                       'cd "$1" || exit 1; if [ -x .venv-wsl/bin/python ]; then exec .venv-wsl/bin/python -m codex_switch --rpc; else exec python3 -m codex_switch --rpc; fi',
                       "codex-switch", root]
        else:
            python = Path(sys.executable)
            if python.name.lower() == "pythonw.exe":
                python = python.with_name("python.exe")
            command = [str(python), "-m", "codex_switch", "--rpc"]
        try:
            p = run(command, input=request.encode(), capture_output=True, cwd=self.root, timeout=60,
                    env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        except (OSError, subprocess.TimeoutExpired):
            raise SwitchError("环境未响应。若切换被中断，请解锁保险库后先执行恢复。") from None
        if p.returncode:
            raise SwitchError("后端无法启动。请在对应环境安装 requirements.txt 中的依赖；WSL 请运行 setup-wsl.sh。")
        try:
            result = json.loads(p.stdout.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise SwitchError("后端返回格式异常，未输出可能包含敏感信息的原始内容。") from None
        if not result.get("ok"):
            raise SwitchError(result.get("error", "操作失败。"))
        return result["result"]
