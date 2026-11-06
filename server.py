"""仅在本机回环地址提供独立程序界面，令牌保护全部操作。"""
import http.server
import json
import os
import pathlib
import secrets
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import webbrowser

import project_sync
import runtime
import sync_core as core
import transport

TOKEN = secrets.token_urlsafe(32)
LOCK = threading.Lock()
JOB = {"busy": False, "message": "准备就绪", "result": None, "error": None}
CHALLENGES = {}
SERVER = None


def records(name):
    return core.read_json(runtime.STATE / (name + ".json"), [])


def save_record(name, value):
    values = records(name)
    values = [item for item in values if item["id"] != value["id"]] + [value]
    core.save_json(runtime.STATE / (name + ".json"), values)
    return value


def save_connection(value):
    connection = transport.validate_connection(value)
    connection["id"] = connection.get("id") or uuid.uuid4().hex
    previous = next((item for item in records("connections") if item["id"] == connection["id"]), {})
    if "device" not in connection and all(previous.get(key) == connection[key] for key in ["address", "port", "user", "platform"]) and "device" in previous:
        connection["device"] = previous["device"]
    save_record("connections", connection)
    core.save_json(runtime.STATE / "ui-preferences.json", {"last_connection": connection["id"]})
    return connection


def choose_path(kind):
    if os.name == "nt":
        import tkinter
        from tkinter import filedialog
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            return filedialog.askopenfilename(title="选择 SSH 私钥文件") if kind == "key" else filedialog.askdirectory(title="选择项目目录")
        finally:
            root.destroy()
    script = 'POSIX path of (choose file with prompt "选择 SSH 私钥文件")' if kind == "key" else 'POSIX path of (choose folder with prompt "选择项目目录")'
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def api(operation, data):
    if operation == "exit":
        if SERVER is not None:
            threading.Timer(0.2, SERVER.shutdown).start()
        return {"closed": True}
    if operation == "catalog":
        return runtime.catalog()
    if operation == "devices":
        return transport.discover(data.get("mode", "tailscale"))
    if operation == "saved":
        profiles = {item["id"]: item for item in records("profiles")}
        for path in (runtime.STATE / "contexts").glob("*/profile.json"):
            value = core.read_json(path)
            profiles.setdefault(value["id"], value)
        return {"connections": records("connections"), "profiles": list(profiles.values()),
                "last_connection": core.read_json(runtime.STATE / "ui-preferences.json", {}).get("last_connection")}
    if operation == "save_connection":
        return save_connection(data["connection"])
    if operation == "choose":
        return {"path": choose_path(data.get("kind"))}
    if operation == "fingerprint":
        connection = transport.validate_connection(data["connection"])
        observed = transport.fingerprint(connection)
        challenge = secrets.token_urlsafe(24)
        CHALLENGES[challenge] = {"connection": connection, "observed": observed, "time": time.monotonic()}
        return {"challenge": challenge, "fingerprints": observed["fingerprints"]}
    if operation == "connect":
        connection = transport.validate_connection(data["connection"])
        challenge = data.get("challenge")
        if challenge:
            entry = CHALLENGES.pop(challenge, None)
            if not entry or time.monotonic() - entry["time"] > 600 or entry["connection"] != connection:
                raise RuntimeError("主机指纹确认已过期，请重新获取")
            transport.trust(connection, entry["observed"])
        if not transport.known_hosts(connection).is_file():
            raise RuntimeError("请先获取并核验目标电脑的 SSH 主机指纹")
        connection = save_connection(connection)
        transport.remember_password(connection, data.get("password", ""))
        remote = transport.rpc(connection, "catalog")
        if remote["device"]["platform"] != connection["platform"]:
            raise RuntimeError("目标系统与选择不一致，请修改系统选项")
        connection["device"] = remote["device"]
        save_connection(connection)
        return {"connection": connection, "catalog": remote}
    if operation == "save_profile":
        profile = data["profile"]
        if not profile.get("mappings"):
            raise RuntimeError("请至少选择一个项目")
        runtime.activate(profile)
        save_record("profiles", profile)
        return profile
    if operation in ["run", "recover"]:
        with LOCK:
            if JOB["busy"]:
                raise RuntimeError("已有同步正在执行")
            profile = data["profile"]
            connection = next((item for item in records("connections") if item["id"] == data["connection_id"]), None)
            if connection is None:
                raise RuntimeError("请先连接目标设备")
            direction = data.get("direction")
            if direction not in ["pull", "push"]:
                raise RuntimeError("请选择同步方向")
            runtime.activate(profile)
            save_record("profiles", profile)
            JOB.update(busy=True, message="正在读取项目映射", result=None, error=None)
        def work():
            try:
                if operation == "recover":
                    result = project_sync.recover_profile(profile) if direction == "pull" else transport.rpc(connection, "recover", profile=profile)
                else:
                    result = project_sync.synchronize(connection, profile, direction, bool(data.get("preview")),
                                                      progress=lambda message: JOB.update(message=message))
                message = "检查完成" if data.get("preview") else "同步完成，临时数据已清理"
                if result.get("cleanup_warning"):
                    message = result["cleanup_warning"]
                JOB.update(message=message, result=result)
            except Exception as error:
                JOB.update(message="操作已停止", error=str(error))
            finally:
                JOB["busy"] = False
                core.save_json(runtime.STATE / "last-result.json", {key: value for key, value in JOB.items()})
        threading.Thread(target=work, daemon=True).start()
        return {"started": True}
    raise RuntimeError("未知界面操作")


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, value, status=200, html=False):
        text = value if html else json.dumps(value, ensure_ascii=False)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(text.encode("utf-8"))

    def authorized(self):
        return secrets.compare_digest(self.headers.get("X-Project-Sync-Token", ""), TOKEN)

    def do_GET(self):
        if self.path == "/":
            path = pathlib.Path(getattr(sys, "_MEIPASS", pathlib.Path(__file__).parent)) / "界面.html"
            self.reply(path.read_text(encoding="utf-8"), html=True)
        elif self.path == "/status" and self.authorized():
            self.reply(JOB)
        elif self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
        else:
            self.reply({"error": "访问令牌无效"}, 403)

    def do_POST(self):
        if not self.path.startswith("/api/") or not self.authorized():
            self.reply({"error": "访问令牌无效"}, 403)
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1048576:
            self.reply({"error": "请求过大"}, 400)
            return
        try:
            if JOB["busy"]:
                raise RuntimeError("同步正在执行，请等待完成后修改设备或项目")
            data = json.loads(self.rfile.read(length))
            self.reply({"ok": True, "result": api(self.path[5:], data)})
        except Exception as error:
            self.reply({"ok": False, "error": str(error)}, 400)


def serve(open_browser=True):
    global SERVER
    runtime.settings()
    session_path = runtime.STATE / "ui-session.json"
    previous = core.read_json(session_path, {})
    if previous:
        try:
            request = urllib.request.Request(previous["url"] + "status", headers={"X-Project-Sync-Token": previous["token"]})
            with urllib.request.urlopen(request, timeout=2) as response:
                if response.status == 200:
                    if open_browser:
                        webbrowser.open(previous["url"] + "#" + previous["token"])
                    return
        except OSError:
            pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    SERVER = server
    url = "http://127.0.0.1:" + str(server.server_port) + "/"
    core.save_json(session_path, {"url": url, "token": TOKEN, "pid": os.getpid()})
    if open_browser:
        webbrowser.open(url + "#" + TOKEN)
    try:
        server.serve_forever()
    finally:
        server.server_close()
