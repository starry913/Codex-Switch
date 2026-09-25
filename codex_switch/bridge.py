"""Local subprocess bridge. Secrets travel over stdin, never argv or a network."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

if os.name == "nt":
    import winreg

from .core import SwitchError


def run(command, **kwargs):
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        kwargs.setdefault("startupinfo", startupinfo)
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
    return subprocess.run(command, **kwargs)


def distributions():
    if os.name != "nt":
        return []
    names = []
    try:
        path = r"Software\Microsoft\Windows\CurrentVersion\Lxss"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as root:
            for index in range(winreg.QueryInfoKey(root)[0]):
                with winreg.OpenKey(root, winreg.EnumKey(root, index)) as distro:
                    name = str(winreg.QueryValueEx(distro, "DistributionName")[0]).strip()
                    if name and not name.lower().startswith("docker-desktop"):
                        names.append(name)
    except OSError:
        pass
    if names:
        return sorted(set(names), key=str.casefold)
    try:
        p = run(["wsl.exe", "--list", "--quiet"], capture_output=True, timeout=10)
        if not p.returncode:
            output = p.stdout.decode("utf-16-le" if b"\x00" in p.stdout else "utf-8", errors="replace")
            return [name.strip() for name in output.splitlines()
                    if name.strip() and not name.strip().lower().startswith("docker-desktop")]
    except (OSError, subprocess.TimeoutExpired):
        pass
    return []


class Bridge:
    def __init__(self, distro=None, home=None, store=None):
        self.distro, self.home, self.store = distro, home, store
        self.frozen = bool(getattr(sys, "frozen", False))
        self.root = self._source_root()

    def _source_root(self):
        """Find the source tree used by the WSL helper after Windows packaging."""
        candidates = [Path.cwd(), Path(__file__).resolve().parent.parent]
        if self.frozen:
            executable = Path(sys.executable).resolve()
            candidates.extend([executable.parent, *executable.parents])
        for candidate in candidates:
            if (candidate / "codex_switch" / "__main__.py").is_file():
                return candidate
        return Path(__file__).resolve().parent.parent

    def call(self, action, **args):
        payload = {"action": action, "home": self.home, "store": self.store, "args": args}
        if self.distro:
            request = json.dumps(payload, ensure_ascii=False)
            if not (self.root / "codex_switch" / "__main__.py").is_file():
                raise SwitchError("软件可以切换 Windows；若要使用 WSL2，请保留 Codex Switch 项目目录。")
            # `wsl -- command` waits on this machine and never returns. `-e` executes directly.
            mapped = run(["wsl.exe", "-d", self.distro, "-e", "wslpath", "-u", str(self.root)], capture_output=True, timeout=15)
            if mapped.returncode:
                raise SwitchError("无法访问 WSL 内的工具目录。请检查发行版是否可启动。")
            root = mapped.stdout.decode("utf-8").strip()
            # Positional arguments avoid interpolating user paths into shell code.
            command = ["wsl.exe", "-d", self.distro, "-e", "sh", "-c",
                       'cd "$1" || exit 1; if [ -x .venv-wsl/bin/python ]; then exec .venv-wsl/bin/python -m codex_switch --rpc; else exec python3 -m codex_switch --rpc; fi',
                       "codex-switch", root]
        elif self.frozen:
            # A windowed PyInstaller executable has no console streams. Run the
            # Windows backend in this worker thread instead of spawning the EXE.
            from .cli import dispatch
            return dispatch(payload)
        else:
            request = json.dumps(payload, ensure_ascii=False)
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
