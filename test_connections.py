"""验证密码 SSH、SFTP 续传、主机核验以及设备设置持久化。"""
import json
import logging
import os
import pathlib
import secrets
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

import paramiko
import runtime
import server
import transport


class PasswordSSH(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.patch = mock.patch.object(runtime, "STATE", self.root)
        self.patch.start()
        self.log_patch = mock.patch.object(logging.getLogger("paramiko.transport"), "disabled", True)
        self.log_patch.start()
        self.password = secrets.token_urlsafe(24)
        self.host_key = paramiko.RSAKey.generate(2048)
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.listener.settimeout(.2)
        self.stopping = threading.Event()
        self.sessions = []
        self.workers = []
        self.connection = {"id": "a" * 32, "address": "127.0.0.1", "port": self.listener.getsockname()[1],
                           "user": "tester", "platform": "macos", "auth": "password", "key": ""}
        known = transport.known_hosts(self.connection)
        known.parent.mkdir()
        known.write_text("[127.0.0.1]:" + str(self.connection["port"]) + " " + self.host_key.get_name() +
                         " " + self.host_key.get_base64() + "\n", encoding="utf-8")
        transport.remember_password(self.connection, self.password)
        self.worker = threading.Thread(target=self.accept, daemon=True)
        self.worker.start()

    def accept(self):
        while not self.stopping.is_set():
            try:
                client, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            worker = threading.Thread(target=self.serve, args=(client,), daemon=True)
            self.workers.append(worker)
            worker.start()

    def serve(self, client):
        expected = self.password
        root = self.root

        class Authentication(paramiko.ServerInterface):
            def check_auth_password(self, username, password):
                return paramiko.AUTH_SUCCESSFUL if username == "tester" and password == expected else paramiko.AUTH_FAILED

            def get_allowed_auths(self, username):
                return "password"

            def check_channel_request(self, kind, channel_id):
                return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

            def check_channel_exec_request(self, channel, command):
                def reply():
                    data = bytearray()
                    while True:
                        block = channel.recv(65536)
                        if not block:
                            break
                        data.extend(block)
                    request = json.loads(data)
                    result = {"operation": request["op"]}
                    if request["op"] == "catalog":
                        result = {"device": {"platform": "macos", "name": "密码测试服务", "device_id": "test-device",
                                             "home": "/Users/tester", "codex_home": "/Users/tester/.codex"},
                                  "projects": [], "default_parent": "/Users/tester/Documents"}
                    channel.sendall(json.dumps({"ok": True, "result": result}).encode())
                    channel.send_exit_status(0)
                    channel.shutdown_write()
                threading.Thread(target=reply, daemon=True).start()
                return True

        class Files(paramiko.SFTPServerInterface):
            def stat(self, path):
                try:
                    return paramiko.SFTPAttributes.from_stat((root / pathlib.PurePosixPath(path).name).stat())
                except OSError as error:
                    return paramiko.SFTPServer.convert_errno(error.errno)

            def open(self, path, flags, attributes):
                try:
                    descriptor = os.open(root / pathlib.PurePosixPath(path).name, flags, 0o600)
                    stream = os.fdopen(descriptor, "r+b" if flags & os.O_RDWR else "wb" if flags & os.O_WRONLY else "rb")
                    handle = paramiko.SFTPHandle(flags)
                    handle.readfile = handle.writefile = stream
                    return handle
                except OSError as error:
                    return paramiko.SFTPServer.convert_errno(error.errno)

        session = paramiko.Transport(client)
        self.sessions.append(session)
        session.add_server_key(self.host_key)
        session.set_subsystem_handler("sftp", paramiko.SFTPServer, Files)
        try:
            session.start_server(server=Authentication())
            while session.is_active() and not self.stopping.is_set():
                time.sleep(.02)
        except (EOFError, paramiko.SSHException):
            pass
        finally:
            session.close()

    def tearDown(self):
        self.stopping.set()
        self.listener.close()
        for session in self.sessions:
            session.close()
        self.worker.join(timeout=2)
        for worker in self.workers:
            worker.join(timeout=2)
        transport.PASSWORDS.clear()
        self.patch.stop()
        self.log_patch.stop()
        self.temporary.cleanup()

    def test_password_rpc_uses_real_ssh_authentication(self):
        result = transport.rpc(self.connection, "hello")
        self.assertEqual(result, {"operation": "hello"})

    def test_wrong_password_fails_without_echo(self):
        transport.remember_password(self.connection, secrets.token_urlsafe(24))
        with self.assertRaisesRegex(RuntimeError, "密码验证失败"):
            transport.rpc(self.connection, "hello")

    def test_untrusted_host_is_rejected(self):
        transport.known_hosts(self.connection).write_text("", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "SSH 连接失败"):
            transport.rpc(self.connection, "hello")

    def test_password_sftp_upload_download_and_resume(self):
        content = secrets.token_bytes(200000)
        source = self.root / "source.bin"
        source.write_bytes(content)
        remote = self.root / "remote.bin"
        transport.transfer(self.connection, "put", source, "/remote.bin", lambda _: None)
        self.assertEqual(remote.read_bytes(), content)
        remote.write_bytes(content[:70000])
        transport.transfer(self.connection, "put", source, "/remote.bin", lambda _: None, resume=True)
        self.assertEqual(remote.read_bytes(), content)
        target = self.root / "target.bin"
        transport.transfer(self.connection, "get", target, "/remote.bin", lambda _: None)
        self.assertEqual(target.read_bytes(), content)
        target.write_bytes(content[:70000])
        transport.transfer(self.connection, "get", target, "/remote.bin", lambda _: None, resume=True)
        self.assertEqual(target.read_bytes(), content)


class SavedConnections(unittest.TestCase):
    def test_save_offline_and_reload_without_password(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(runtime, "STATE", pathlib.Path(temporary)):
            value = {"name": "家里的电脑", "address": "192.168.1.10", "user": "tester", "platform": "windows",
                     "port": 2222, "auth": "password", "password": secrets.token_urlsafe(24), "network": "lan"}
            saved = server.api("save_connection", {"connection": value})
            self.assertNotIn("password", saved)
            loaded = server.api("saved", {})
            self.assertEqual(loaded["connections"], [saved])
            self.assertEqual(loaded["last_connection"], saved["id"])
            self.assertNotIn(value["password"], (pathlib.Path(temporary) / "connections.json").read_text(encoding="utf-8"))
            server.api("save_connection", {"connection": dict(saved, port=22)})
            self.assertEqual(len(server.api("saved", {})["connections"]), 1)

    def test_legacy_key_settings_remain_compatible(self):
        value = transport.validate_connection({"address": "127.0.0.1", "user": "tester", "platform": "macos"})
        self.assertEqual(value["auth"], "key")


if __name__ == "__main__":
    unittest.main()
