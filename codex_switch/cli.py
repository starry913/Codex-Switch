from __future__ import annotations

import argparse
import getpass
import json
import os
import sys

from .core import Manager, SwitchError


def dispatch(request):
    allowed = {"diagnostic", "history", "capture", "add_api", "profiles", "preview", "apply", "restore", "quick_switch"}
    action = request.get("action")
    if action not in allowed:
        raise SwitchError("未知操作。")
    manager = Manager(request.get("home"), request.get("store"))
    return getattr(manager, action)(**request.get("args", {}))


def main():
    parser = argparse.ArgumentParser(description="Codex Switch · 在当前环境切换连接，保留本地工作数据")
    parser.add_argument("--home", help="指定此环境的 CODEX_HOME，不复制或迁移数据")
    parser.add_argument("--store", help="加密保险库目录，默认 CODEX_HOME/switch-local")
    parser.add_argument("--rpc", action="store_true", help=argparse.SUPPRESS)
    subs = parser.add_subparsers(dest="command")
    subs.add_parser("gui", help="打开桌面窗口")
    subs.add_parser("doctor", help="只读检查本地环境")
    subs.add_parser("history", help="只读列出历史会话，不按服务过滤")
    subs.add_parser("list", help="解锁并列出保存的服务")
    capture = subs.add_parser("capture", help="加密导入当前服务配置及文件型登录")
    capture.add_argument("name")
    add = subs.add_parser("add", help="添加兼容 Responses 的 API 服务，密钥通过隐藏输入读取")
    add.add_argument("name")
    add.add_argument("--url", required=True)
    add.add_argument("--model", required=True)
    preview = subs.add_parser("preview", help="预览切换，不修改配置")
    preview.add_argument("name")
    switch = subs.add_parser("switch", help="切换服务，自动加密备份；必须关闭 Codex")
    switch.add_argument("name")
    subs.add_parser("restore", help="恢复上一次切换，必须关闭 Codex")
    options = parser.parse_args()
    if options.rpc:
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
        try:
            request = json.loads(sys.stdin.read(2 * 1024 * 1024))
            response = {"ok": True, "result": dispatch(request)}
        except SwitchError as e:
            response = {"ok": False, "error": str(e)}
        except PermissionError:
            response = {"ok": False, "error": "配置文件是只读的，或被其他程序占用，这次没有切换。"}
        except Exception:
            response = {"ok": False, "error": "操作未完成。请检查目录权限、依赖和配置格式；未输出敏感详情。"}
        print(json.dumps(response, ensure_ascii=False))
        return
    if options.command in (None, "gui"):
        from .gui import launch
        launch(options.home, options.store)
        return
    aliases = {"doctor": "diagnostic", "list": "profiles", "add": "add_api", "switch": "apply"}
    action = aliases.get(options.command, options.command)
    args = {}
    if action not in ("diagnostic", "history"):
        args["password"] = getpass.getpass("保险库密码（至少 10 个字符）: ")
    if hasattr(options, "name"):
        args["name"] = options.name
    if action == "add_api":
        args.update(url=options.url, model=options.model, key=getpass.getpass("API Key（不显示）: "))
    try:
        result = dispatch({"action": action, "home": options.home, "store": options.store, "args": args})
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except SwitchError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
    except OSError:
        print("操作系统拒绝操作，请检查文件权限或进程占用。", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
