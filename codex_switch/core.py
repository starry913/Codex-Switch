from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import psutil
import tomlkit
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt


class SwitchError(Exception):
    """A message safe to present to the user (never includes credential values)."""


# Only these top-level settings are owned by connection profiles.
CONNECTION_KEYS = (
    "model_provider", "model", "review_model", "openai_base_url",
    "chatgpt_base_url", "cli_auth_credentials_store", "forced_login_method",
    "forced_chatgpt_workspace_id",
)
MANAGED_FILES = ("config.toml", "auth.json")
MAX_FILE = 16 * 1024 * 1024


def digest(data):
    return None if data is None else hashlib.sha256(data).hexdigest()


def read_optional(path):
    if path.is_symlink():
        raise SwitchError("配置文件是符号链接，请先确认真实配置位置。")
    if not path.exists():
        return None
    if path.stat().st_size > MAX_FILE:
        raise SwitchError("配置文件异常大，已停止操作。")
    return path.read_bytes()


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".switch-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if os.name == "nt":
            os.chmod(name, stat.S_IREAD | stat.S_IWRITE)
            if path.exists():
                os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
        else:
            os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def parse_config(raw):
    try:
        return tomlkit.parse((raw or b"").decode("utf-8-sig"))
    except Exception:
        raise SwitchError("config.toml 无法解析，未修改任何配置。") from None


def parse_auth(raw):
    if raw is None:
        raise SwitchError("没有文件型登录凭据。请先在对应环境使用 Codex 原生登录，再导入。")
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except Exception:
        raise SwitchError("auth.json 格式无效。") from None


def validate_url(url):
    try:
        p = urlsplit(url)
        _ = p.port
    except ValueError:
        raise SwitchError("服务地址格式无效。") from None
    if not p.hostname or p.username or p.password or p.query or p.fragment:
        raise SwitchError("服务地址必须包含主机名，不能包含密钥、查询参数或片段。")
    if p.scheme != "https" and not (p.scheme == "http" and p.hostname in {"localhost", "127.0.0.1", "::1"}):
        raise SwitchError("远程服务必须使用 HTTPS；HTTP 仅用于本机代理。")


def is_switch_client(record):
    """Desktop app and the standalone CLI. Editor extensions restart themselves."""
    name = (record.get("name") or "").lower()
    exe = (record.get("exe") or "").replace("/", "\\").lower()
    if name in {"chatgpt", "chatgpt.exe", "codex-desktop", "codex-desktop.exe"}:
        return True
    if name in {"codex", "codex.exe"}:
        # The Linux client inside a WSL Cursor session is the Codex the user is
        # actually talking to. The Windows editor extension restarts on its own
        # and would rewrite the desktop config, so it stays untouched.
        if "\\.cursor-server\\" in exe:
            return True
        if "\\.cursor\\" in exe or "\\.vscode\\" in exe or "\\extensions\\" in exe:
            return False
        return True
    return False


def app_id_from_exe(exe):
    match = re.search(r"OpenAI\.Codex_[^\\]*__([A-Za-z0-9]+)(?:\\|$)", exe or "", re.IGNORECASE)
    if not match:
        return None
    return f"OpenAI.Codex_{match.group(1)}!App"


def active_clients():
    found = []
    for p in psutil.process_iter(["pid", "name"]):
        try:
            name = p.info["name"] or ""
            exe = ""
            if name.lower() in {"codex", "codex.exe"}:
                try:
                    exe = p.exe()
                except (psutil.Error, OSError):
                    exe = ""
            record = {"pid": p.pid, "name": name, "exe": exe}
            if is_switch_client(record):
                found.append(record)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return found


class Vault:
    """AES-GCM encrypted profiles, credentials, recovery journal and snapshots."""

    def __init__(self, directory, password):
        self.directory = Path(directory)
        self.path = self.directory / "profiles.vault"
        if len(password) < 10:
            raise SwitchError("保险库密码至少需要 10 个字符。")
        if self.path.exists():
            try:
                envelope = json.loads(self.path.read_text())
                if envelope["version"] != 1:
                    raise SwitchError("不支持的保险库版本。")
                self.salt = base64.b64decode(envelope["salt"], validate=True)
                self.key = self.derive(password)
                payload = AESGCM(self.key).decrypt(
                    base64.b64decode(envelope["nonce"], validate=True),
                    base64.b64decode(envelope["data"], validate=True), b"codex-switch-v1")
                self.data = json.loads(payload)
            except (InvalidTag, ValueError, KeyError, TypeError):
                raise SwitchError("保险库密码错误，或保险库文件已损坏。") from None
        else:
            self.salt = os.urandom(16)
            self.key = self.derive(password)
            self.data = {"profiles": {}, "backups": [], "pending": None, "active": None}

    def derive(self, password):
        return Scrypt(salt=self.salt, length=32, n=2**15, r=8, p=1).derive(password.encode())

    def save(self):
        nonce = os.urandom(12)
        ciphertext = AESGCM(self.key).encrypt(nonce, json.dumps(self.data).encode(), b"codex-switch-v1")
        atomic_write(self.path, json.dumps({"version": 1,
            "salt": base64.b64encode(self.salt).decode(),
            "nonce": base64.b64encode(nonce).decode(),
            "data": base64.b64encode(ciphertext).decode()}).encode())


class Manager:
    def __init__(self, home=None, store=None, process_check=active_clients):
        self.home = Path(home or os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve()
        self.store = Path(store).expanduser().resolve() if store else self.home / "switch-local"
        self.process_check = process_check

    @contextlib.contextmanager
    def locked(self):
        self.store.mkdir(parents=True, exist_ok=True, mode=0o700)
        lockpath = self.store / "operation.lock"
        # Kernel locks are released on process exit, including crashes.
        f = open(lockpath, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0, 2)
                if f.tell() == 0:
                    f.write(b"0")
                    f.flush()
                f.seek(0)
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    raise SwitchError("另一个切换操作正在进行。") from None
            else:
                import fcntl
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    raise SwitchError("另一个切换操作正在进行。") from None
            yield
        finally:
            f.close()

    def files(self):
        return {name: read_optional(self.home / name) for name in MANAGED_FILES}

    def diagnostic(self):
        files = self.files()
        c = parse_config(files["config.toml"])
        provider = str(c.get("model_provider", "openai"))
        p = c.get("model_providers", {}).get(provider, {})
        endpoint = p.get("base_url", c.get("openai_base_url", ""))
        auth_kind = "未检测到"
        if files["auth.json"]:
            a = parse_auth(files["auth.json"])
            auth_kind = "官方登录" if a.get("tokens") else "API Key" if a.get("OPENAI_API_KEY") else "其他"
        codex_path = shutil.which("codex")
        warnings = []
        if os.environ.get("WSL_DISTRO_NAME") and codex_path and codex_path.startswith("/mnt/"):
            warnings.append("WSL 的 Codex 启动器来自 Windows 挂载路径；请确认实际运行的是 Linux Codex，避免改错环境。")
        overrides = [k for k in ("OPENAI_API_KEY", "OPENAI_BASE_URL") if os.environ.get(k)]
        if overrides:
            warnings.append("检测到可能覆盖文件配置的环境变量：" + ", ".join(overrides) + "。已打开客户端的命令行覆盖也可能优先于文件配置。")
        return {"home": str(self.home), "exists": self.home.is_dir(), "provider": provider,
                "model": str(c.get("model", "未指定")), "host": urlsplit(endpoint).hostname,
                "codex_path": codex_path, "wsl_distro": os.environ.get("WSL_DISTRO_NAME"), "warnings": warnings,
                "auth_kind": auth_kind, "credential_store": str(c.get("cli_auth_credentials_store", "file")),
                "vault_exists": (self.store / "profiles.vault").exists(),
                "processes": self.process_check(),
                "sessions": sum(1 for _ in (self.home / "sessions").rglob("*.jsonl")),
                "archived_sessions": sum(1 for _ in (self.home / "archived_sessions").rglob("*.jsonl")),
                "memory_present": (self.home / "memories").exists() or any(self.home.glob("memories*.sqlite")),
                "protected": [p.name for p in self.home.iterdir() if p.name not in MANAGED_FILES and p != self.store] if self.home.exists() else []}

    def history(self, limit=100):
        # Read only: no provider remapping or edits to Codex internal tables.
        dbs = sorted(self.home.glob("state_*.sqlite"), key=lambda p: int(re.search(r"(\d+)\.sqlite$", p.name)[1]), reverse=True)
        if not dbs:
            return []
        try:
            with contextlib.closing(sqlite3.connect(dbs[0].as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
                cols = {r[1] for r in db.execute("pragma table_info(threads)")}
                required = {"id", "title", "cwd", "model_provider"}
                if not required <= cols:
                    raise SwitchError("当前版本的历史索引格式暂不支持；原始数据未修改。")
                order = "updated_at" if "updated_at" in cols else "id"
                rows = db.execute(f"SELECT id,title,cwd,model_provider FROM threads ORDER BY {order} DESC LIMIT ?", (min(max(int(limit), 1), 500),)).fetchall()
                return [dict(zip(("id", "title", "cwd", "provider"), row)) for row in rows]
        except sqlite3.Error:
            raise SwitchError("历史索引暂不可读，请关闭 Codex 后重试。") from None

    def capture(self, name, password):
        with self.locked():
            v = Vault(self.store, password)
            self.require_ready(v)
            self.validate_name(name, v)
            files = self.files()
            files = self.files()
            c = parse_config(files["config.toml"])
            if c.get("cli_auth_credentials_store", "file") != "file":
                raise SwitchError("当前使用系统凭据库或 auto 模式，首版不复制该凭据。请使用文件型凭据配置后再导入。")
            auth = parse_auth(files["auth.json"])
            provider = str(c.get("model_provider", "openai"))
            settings = {k: c[k].unwrap() if hasattr(c[k], "unwrap") else c[k] for k in CONNECTION_KEYS if k in c}
            settings["cli_auth_credentials_store"] = "file"
            custom = c.get("model_providers", {}).get(provider)
            if provider != "openai" and custom is None:
                raise SwitchError("当前服务配置不完整，无法导入。")
            if custom and (custom.get("env_key") or custom.get("auth") or custom.get("env_http_headers")):
                raise SwitchError("当前连接依赖环境变量或外部认证程序，首版不保存这些外部依赖。")
            if provider == "openai" and not (auth.get("tokens") or auth.get("OPENAI_API_KEY")):
                raise SwitchError("未找到可保存的官方登录或 API Key。")
            v.data["profiles"][name] = {"settings": settings, "provider": provider,
                "definition": custom.unwrap() if custom else None,
                "auth": base64.b64encode(files["auth.json"]).decode(),
                "kind": "official" if provider == "openai" and auth.get("tokens") else "api",
                "created": time.time()}
            v.data["active"] = name
            v.save()
            return {"message": f"已加密保存「{name}」，当前 Codex 配置未改变。"}

    @staticmethod
    def validate_name(name, vault):
        if not isinstance(name, str) or not name.strip() or len(name) > 60 or any(ord(c) < 32 for c in name):
            raise SwitchError("名称需要 1–60 个可见字符。")
        if name in vault.data["profiles"]:
            raise SwitchError("名称已存在，请使用其他名称。")

    @staticmethod
    def require_ready(v):
        if v.data.get("pending"):
            raise SwitchError("上次切换被中断。请先执行恢复，不能开始新的切换。")

    def add_api(self, name, url, key, model, password):
        validate_url(url)
        if not key.strip() or any(c in key for c in "\r\n"):
            raise SwitchError("请输入有效的 API Key。")
        if not model.strip() or any(ord(c) < 32 for c in model):
            raise SwitchError("请填写服务实际支持的模型名称。")
        with self.locked():
            v = Vault(self.store, password)
            self.require_ready(v)
            self.validate_name(name, v)
            # Stable identity for API profiles created by this tool. The endpoint
            # and credentials change, not the session's provider namespace.
            provider = "switch_local"
            v.data["profiles"][name] = {"kind": "api", "created": time.time(), "provider": provider,
                "settings": {"model_provider": provider, "model": model.strip(), "cli_auth_credentials_store": "file"},
                "definition": {"name": name, "base_url": url.rstrip("/"), "wire_api": "responses", "requires_openai_auth": True},
                "auth": base64.b64encode(json.dumps({"OPENAI_API_KEY": key.strip()}).encode()).decode()}
            v.save()
            return {"message": f"已保存「{name}」。保存不会发送请求或消耗额度。"}

    def profiles(self, password):
        v = Vault(self.store, password)
        return {"active": v.data.get("active"), "pending": bool(v.data.get("pending")),
                "backups": len(v.data["backups"]),
                "profiles": [{"name": n, "kind": p["kind"], "provider": p["provider"],
                              "model": p["settings"].get("model", "默认"),
                              "host": urlsplit((p.get("definition") or {}).get("base_url", p["settings"].get("openai_base_url", ""))).hostname}
                             for n, p in v.data["profiles"].items()]}

    def target(self, profile, files):
        c = parse_config(files["config.toml"])
        # Mutate the TOML syntax tree: preserve unrelated tables and comments.
        for key in CONNECTION_KEYS:
            c.pop(key, None)
        for key, value in profile["settings"].items():
            c[key] = value
        if profile.get("definition") is not None:
            if "model_providers" not in c:
                c["model_providers"] = tomlkit.table()
            c["model_providers"][profile["provider"]] = profile["definition"]
        raw = tomlkit.dumps(c).encode()
        parse_config(raw)
        return {"config.toml": raw, "auth.json": base64.b64decode(profile["auth"])}

    def preview(self, name, password):
        v = Vault(self.store, password)
        self.require_ready(v)
        if name not in v.data["profiles"]:
            raise SwitchError("所选服务不存在。")
        before = self.files()
        target = self.target(v.data["profiles"][name], before)
        return {"message": "仅预览，尚未切换。", "changed_files": [n for n in MANAGED_FILES if before[n] != target[n]],
                "from": str(parse_config(before["config.toml"]).get("model_provider", "openai")),
                "to": v.data["profiles"][name]["provider"], "home": str(self.home),
                "processes": self.process_check(), "history_policy": "保留原文件和索引，不改写历史服务标记；跨服务原生续聊需验证。"}

    def ensure_idle(self):
        processes = self.process_check()
        if processes:
            names = ", ".join(f"{p['name']} ({p['pid']})" for p in processes[:6])
            raise SwitchError(f"请先退出桌面 Codex、CLI 和 VS Code 中的 Codex 后台进程，然后重试：{names}")

    def apply(self, name, password):
        with self.locked():
            self.ensure_idle()
            v = Vault(self.store, password)
            self.require_ready(v)
            if name not in v.data["profiles"]:
                raise SwitchError("所选服务不存在。")
            before = self.files()
            current = parse_config(before["config.toml"])
            if current.get("cli_auth_credentials_store", "file") != "file":
                raise SwitchError("当前配置使用系统凭据库或 auto 模式，首版不切换，以免无法完整恢复登录状态。")
            # Persist refreshed OAuth tokens for the outgoing profile, only when the
            # current connection matches its expected settings and credential mode.
            old_name = v.data.get("active")
            old = v.data["profiles"].get(old_name)
            if old and before["auth.json"] and all(current.get(k) == val for k, val in old["settings"].items() if k != "cli_auth_credentials_store") and current.get("model_providers", {}).get(old["provider"]) == old.get("definition"):
                a = parse_auth(before["auth.json"])
                if old["kind"] == "official" and a.get("tokens"):
                    old["auth"] = base64.b64encode(before["auth.json"]).decode()
            target = self.target(v.data["profiles"][name], before)
            if before == target:
                return {"message": "所选服务配置已经生效，无需修改。"}
            backup = {"id": uuid.uuid4().hex, "created": time.time(), "previous_active": old_name,
                      "before": {k: base64.b64encode(val).decode() if val is not None else None for k, val in before.items()},
                      "after": {k: digest(val) for k, val in target.items()}}
            v.data["backups"].append(backup)
            v.data["pending"] = backup["id"]
            v.save()  # durable encrypted recovery journal BEFORE touching Codex
            try:
                self.ensure_idle()
                if self.files() != before:
                    raise SwitchError("配置在预备切换时被其他程序修改，已取消。")
            except Exception:
                v.data["pending"] = None
                v.data["backups"].pop()
                v.save()
                raise
            try:
                for n in MANAGED_FILES:
                    if read_optional(self.home / n) != before[n]:
                        raise SwitchError("配置被其他程序修改。")
                    atomic_write(self.home / n, target[n])
                if self.files() != target:
                    raise SwitchError("配置写入校验失败。")
            except Exception:
                # Keep the pending journal if rollback itself fails.
                now = self.files()
                if any(now[n] not in (before[n], target[n]) for n in MANAGED_FILES):
                    raise SwitchError("检测到外部配置修改，已停止覆盖。加密恢复记录已保留，请检查后恢复。") from None
                self.write_snapshot(before)
                v.data["pending"] = None
                v.data["backups"].pop()
                v.save()
                raise SwitchError("切换失败，已恢复切换前的配置。") from None
            v.data["active"] = name
            v.data["pending"] = None
            v.save()
            return {"message": f"已切换到「{name}」。请重新打开 Codex / 重载插件。原有会话、记忆和项目文件未修改。",
                    "backup": backup["id"], "changed_files": list(MANAGED_FILES), "continuation_verified": False}

    def write_snapshot(self, snapshot):
        for n, val in snapshot.items():
            if val is None:
                (self.home / n).unlink(missing_ok=True)
            else:
                atomic_write(self.home / n, val)

    def restore(self, password):
        with self.locked():
            self.ensure_idle()
            v = Vault(self.store, password)
            if not v.data["backups"]:
                raise SwitchError("没有可恢复的切换备份。")
            b = v.data["backups"][-1]
            current = self.files()
            before = {k: base64.b64decode(val) if val is not None else None for k, val in b["before"].items()}
            # A pending transaction can contain either old or new bytes per file.
            for n in MANAGED_FILES:
                allowed = {b["after"][n]}
                if v.data.get("pending"):
                    allowed.add(digest(before[n]))
                if digest(current[n]) not in allowed:
                    raise SwitchError("切换后配置或凭据已被其他程序修改。为避免覆盖新登录状态，自动恢复已停止。")
            v.data["pending"] = b["id"]
            v.save()
            self.write_snapshot(before)
            v.data["active"] = b["previous_active"]
            v.data["pending"] = None
            v.data["backups"].pop()
            v.save()
            return {"message": "已恢复切换前的配置和登录状态。会话、记忆及项目文件未修改。"}

    def _load_quick_keys(self):
        key_file = self.store / "quick_keys.json"
        if not key_file.exists():
            return {}
        try:
            saved = json.loads(key_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise SwitchError("本地服务配置文件无法读取。") from None
        if not isinstance(saved, dict):
            raise SwitchError("本地服务配置文件无法读取。")
        return saved

    def _save_quick_keys(self, keys):
        self.store.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write(self.store / "quick_keys.json", json.dumps({
            "packy": keys.get("packy") or "",
            "rightcode": keys.get("rightcode") or "",
        }, ensure_ascii=False).encode())

    def _absorb_live_connection(self, files, keys):
        """Keep the key that is actually working before another preset overwrites it."""
        config = parse_config(files["config.toml"])
        provider = str(config.get("model_provider") or "openai")
        auth = {}
        if files["auth.json"]:
            try:
                parsed = json.loads(files["auth.json"])
                if isinstance(parsed, dict):
                    auth = parsed
            except ValueError:
                auth = {}
        live_key = auth.get("OPENAI_API_KEY")
        if provider in {"packycode", "packy"} and isinstance(live_key, str) and live_key.strip():
            keys["packy"] = live_key.strip()
            self._save_quick_keys(keys)
        elif provider == "rightcode" and isinstance(live_key, str) and live_key.strip():
            keys["rightcode"] = live_key.strip()
            self._save_quick_keys(keys)
        elif provider == "openai" and isinstance(auth.get("tokens"), dict) and auth.get("tokens"):
            atomic_write(self.store / "official_auth.json", files["auth.json"])

    def _blocking_clients(self):
        return [item for item in self.process_check() if is_switch_client(item)]

    def stop_clients(self):
        """Stop the desktop app and standalone CLI before either config file changes.

        Writing a new API key while the old provider is still in memory sends the
        wrong key to the previous service. Editor-owned codex.exe processes are
        ignored because those hosts restart them immediately.
        """
        app_id = None
        for _ in range(3):
            clients = self._blocking_clients()
            if not clients:
                return app_id
            for item in clients:
                app_id = app_id_from_exe(item.get("exe")) or app_id
                pid = item.get("pid")
                if pid is None:
                    continue
                try:
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"], capture_output=True, timeout=8, check=False)
                    else:
                        os.kill(int(pid), 15)
                except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
                    continue
            deadline = time.time() + 2
            while time.time() < deadline:
                if not self._blocking_clients():
                    return app_id
                time.sleep(0.2)
        raise SwitchError("Codex 还在运行，这次没有改配置。请先退出桌面 Codex 后再点切换。")

    def quick_switch(self, service, packy_key=None, rightcode_key=None, restart=False):
        """Passwordless personal mode: apply three fixed local connection presets.

        This intentionally keeps the preset file local and mode 0600. It is for a
        single-user machine; users who need shared/portable secrets should use the
        encrypted profile commands instead.
        """
        keys = self._load_quick_keys()
        if packy_key:
            keys["packy"] = packy_key
        if rightcode_key:
            keys["rightcode"] = rightcode_key
        if service not in {"官方直连", "Packy", "RightCode"}:
            raise SwitchError("未知服务。")
        with self.locked():
            app_id = self.stop_clients() if restart else None
            if not restart:
                self.ensure_idle()
            files = self.files()
            self._absorb_live_connection(files, keys)
            presets = {
                "官方直连": {"provider": "openai", "model": "gpt-6-astra", "endpoint": None, "key": None},
                # Packy retired gpt-5.5 from its Codex group on 2026-09-09.
                # Their current Codex import preset uses gpt-5.6-sol.
                "Packy": {"provider": "packycode", "model": "gpt-5.6-sol", "endpoint": "https://cf.api.fan/v1", "key": keys.get("packy")},
                "RightCode": {"provider": "rightcode", "model": "gpt-6-astra", "endpoint": "https://rightapi.ai/codex/v1", "key": keys.get("rightcode")},
            }
            preset = presets[service]
            if service != "官方直连" and not preset["key"]:
                raise SwitchError(f"还没有配置「{service}」的本地 API Key。")
            c = parse_config(files["config.toml"])
            # Keep all non-connection tables, instructions, plugins and project state.
            for key in CONNECTION_KEYS:
                c.pop(key, None)
            c["model"] = preset["model"]
            if preset["provider"] != "openai":
                c["model_provider"] = preset["provider"]
                c["preferred_auth_method"] = "apikey"
                providers = c.get("model_providers") or tomlkit.table()
                providers[preset["provider"]] = {"name": preset["provider"], "base_url": preset["endpoint"], "requires_openai_auth": True, "wire_api": "responses"}
                c["model_providers"] = providers
                target_auth = json.dumps({"OPENAI_API_KEY": preset["key"]}, indent=2).encode()
            else:
                c.pop("model_provider", None)
                c.pop("preferred_auth_method", None)
                official = self.store / "official_auth.json"
                if official.exists():
                    target_auth = official.read_bytes()
                else:
                    target_auth = b"{}\n"
            target_config = tomlkit.dumps(c).encode()
            provider_name = "openai" if preset["provider"] == "openai" else preset["provider"]
            already = files["config.toml"] == target_config and files["auth.json"] == target_auth
            backup = None
            if not already:
                backup = self.store / "quick-backup.json"
                payload = {n: base64.b64encode(v).decode() if v is not None else None for n, v in files.items()}
                atomic_write(backup, json.dumps(payload).encode())
                try:
                    atomic_write(self.home / "config.toml", target_config)
                    atomic_write(self.home / "auth.json", target_auth)
                    if self.files()["config.toml"] != target_config or self.files()["auth.json"] != target_auth:
                        raise SwitchError("写入校验失败。")
                except Exception:
                    self.write_snapshot(files)
                    raise SwitchError("切换失败，已恢复原配置。") from None
            retargeted = self.retarget_threads(provider_name, preset["model"])
            thread_note = f"{retargeted} 条旧对话已改到这个服务，可以直接继续。" if retargeted else ""
            if already:
                message = f"已经是「{service}」。" + (thread_note or "无需修改。")
                return {"message": message, "app_id": app_id, "restart_paths": [], "retargeted": retargeted}
            message = f"已切换到「{service}」。{thread_note}记忆和项目文件未修改。"
            if service == "官方直连" and b'"tokens"' not in target_auth:
                message = "已切换到「官方直连」。还没有保存过官方登录，打开 Codex 后登录一次，之后再切换会自动记住。" + thread_note
            return {"message": message, "backup": str(backup), "restart_paths": [], "app_id": app_id, "retargeted": retargeted}

    def retarget_threads(self, provider, model):
        """Point existing chats at the connection just selected.

        A resumed thread keeps its own provider and model. Leaving those unchanged
        sends the new API key to the previous service.
        """
        updated = 0
        rollouts = []
        for db_path in sorted(self.home.glob("state_*.sqlite")):
            db = sqlite3.connect(db_path, timeout=5)
            try:
                cols = {row[1] for row in db.execute("pragma table_info(threads)")}
                if not {"model_provider", "model", "rollout_path"} <= cols:
                    continue
                cursor = db.execute(
                    "UPDATE threads SET model_provider=?, model=CASE WHEN model='codex-auto-review' THEN model ELSE ? END "
                    "WHERE model_provider IS NOT ?",
                    (provider, model, provider))
                updated += max(cursor.rowcount, 0)
                # Models may still differ when the provider was already retargeted.
                cursor = db.execute(
                    "UPDATE threads SET model=? WHERE model_provider=? AND model IS NOT ? AND model!='codex-auto-review'",
                    (model, provider, model))
                updated += max(cursor.rowcount, 0)
                rollouts.extend(row[0] for row in db.execute("SELECT rollout_path FROM threads WHERE rollout_path IS NOT NULL"))
                db.commit()
            except sqlite3.OperationalError:
                raise SwitchError("连接已切换，但旧对话索引正被占用。请退出 Codex 后再切换一次。") from None
            finally:
                db.close()
        for rollout in rollouts:
            self.retarget_rollout(rollout, provider)
        return updated

    def retarget_rollout(self, rollout, provider):
        path = Path(str(rollout).removeprefix("\\\\?\\"))
        if not path.is_file() or path.is_symlink():
            return
        try:
            raw = path.read_bytes()
        except OSError:
            return
        split = raw.split(b"\n", 1)
        try:
            meta = json.loads(split[0])
        except ValueError:
            return
        payload = meta.get("payload") if isinstance(meta, dict) else None
        if not isinstance(payload, dict) or payload.get("model_provider") in (None, provider):
            return
        payload["model_provider"] = provider
        first = json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode()
        rest = b"\n" + split[1] if len(split) > 1 else b""
        atomic_write(path, first + rest)

