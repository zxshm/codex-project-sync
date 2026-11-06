"""通过用户选择的 SSH 设备传递结构化请求与更新包。"""
import base64
import hashlib
import ipaddress
import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import contextlib

import runtime
import sync_core as core

PASSWORDS = {}


def validate_connection(value):
    address = value.get("address", "").strip().strip("[]")
    if not address or address.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.:%-]+", address):
        raise RuntimeError("目标地址不合法，请填写 IP 或主机名")
    user = value.get("user", "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.\\-]+", user):
        raise RuntimeError("SSH 用户名不合法")
    port = int(value.get("port", 22))
    if not 1 <= port <= 65535:
        raise RuntimeError("SSH 端口必须在 1 到 65535 之间")
    platform = value.get("platform")
    if platform not in ["windows", "macos"]:
        raise RuntimeError("请选择目标电脑的系统")
    key = value.get("key", "").strip()
    auth = value.get("auth", "key")
    if auth not in ["key", "password"]:
        raise RuntimeError("请选择密钥或密码认证")
    if auth == "key" and key and not pathlib.Path(key).expanduser().is_file():
        raise RuntimeError("找不到指定 SSH 私钥文件")
    allowed = {name: value[name] for name in ["id", "name", "device", "entry", "network"] if name in value}
    return dict(allowed, address=address, user=user, port=port, platform=platform, key=key, auth=auth)


def password_key(connection):
    return (connection.get("id", ""), connection["address"], connection["user"], connection["port"])


def remember_password(connection, password):
    if connection["auth"] == "password" and password:
        PASSWORDS[password_key(connection)] = password


@contextlib.contextmanager
def password_client(connection):
    import paramiko
    password = PASSWORDS.get(password_key(connection))
    if not password:
        raise RuntimeError("请输入 SSH 登录密码。密码仅保存在本次运行内存中，重开程序后需要重新输入")
    client = paramiko.SSHClient()
    try:
        client.load_host_keys(str(known_hosts(connection)))
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        client.connect(connection["address"], port=connection["port"], username=connection["user"],
                       password=password, allow_agent=False, look_for_keys=False,
                       timeout=10, auth_timeout=15, banner_timeout=15)
        client.get_transport().set_keepalive(10)
        yield client
    except paramiko.AuthenticationException:
        raise RuntimeError("密码验证失败，请检查用户名、密码，以及目标 SSH 是否允许密码登录") from None
    except paramiko.SSHException:
        raise RuntimeError("SSH 连接失败，请检查网络、主机指纹和目标 SSH 服务") from None
    finally:
        client.close()


def password_rpc(connection, request):
    with password_client(connection) as client:
        stdin, stdout, stderr = client.exec_command(agent_command(connection), timeout=900)
        stdin.write(json.dumps(request, ensure_ascii=False))
        stdin.flush()
        stdin.channel.shutdown_write()
        channel = stdout.channel
        output = bytearray()
        deadline = time.monotonic() + 900
        while True:
            if channel.recv_ready():
                output.extend(channel.recv(1048576))
            if channel.recv_stderr_ready():
                channel.recv_stderr(1048576)
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                break
            if time.monotonic() > deadline:
                raise RuntimeError("目标接收组件响应超时")
            time.sleep(0.01)
        if channel.recv_exit_status():
            raise RuntimeError("目标接收组件执行失败，请检查目标程序安装位置")
        return output.decode("utf-8")


def password_transfer(connection, direction, local, remote, progress, resume):
    with password_client(connection) as client, client.open_sftp() as sftp:
        sftp.get_channel().settimeout(60)
        offset = (local.stat().st_size if local.exists() else 0) if direction == "get" else (sftp.stat(remote).st_size if resume else 0)
        offset = offset if resume else 0
        total = sftp.stat(remote).st_size if direction == "get" else local.stat().st_size
        if offset > total:
            raise RuntimeError("续传断点超过源文件长度，请重新检查更新")
        source = sftp.open(remote, "rb") if direction == "get" else local.open("rb")
        target = local.open("ab" if resume else "wb") if direction == "get" else sftp.open(remote, "r+b" if resume else "wb")
        with source, target:
            source.seek(offset)
            target.seek(offset)
            transferred = offset
            started = time.monotonic()
            while True:
                block = source.read(1048576)
                if not block:
                    break
                target.write(block)
                transferred += len(block)
                progress("正在传输更新包 · " + str(round(transferred / 1048576, 1)) + " / " + str(round(total / 1048576, 1)) +
                         " MiB · 已用 " + str(int(time.monotonic() - started)) + " 秒")
            if transferred != total:
                raise RuntimeError("源文件长度在传输中发生变化，请重新检查更新")


def known_hosts(connection):
    identifier = hashlib.sha256((connection["address"] + ":" + str(connection["port"])).encode()).hexdigest()[:20]
    return runtime.STATE / "hosts" / (identifier + ".known_hosts")


def options(connection, sftp=False):
    result = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=10",
              "-o", "ServerAliveCountMax=3", "-o", "StrictHostKeyChecking=yes",
              "-o", "UserKnownHostsFile=" + str(known_hosts(connection)), "-P" if sftp else "-p", str(connection["port"])]
    if connection.get("key"):
        result += ["-i", str(pathlib.Path(connection["key"]).expanduser()), "-o", "IdentitiesOnly=yes"]
    return result


def fingerprint(connection):
    connection = validate_connection(connection)
    result = subprocess.run(["ssh-keyscan", "-T", "5", "-p", str(connection["port"]), "-t", "ed25519", connection["address"]],
                            capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    lines = [line for line in result.stdout.splitlines() if line and not line.startswith("#")]
    if not lines:
        raise RuntimeError("目标 SSH 没有响应，请检查地址、端口和远程登录")
    fingerprints = []
    for line in lines:
        parts = line.split()
        digest = base64.b64encode(hashlib.sha256(base64.b64decode(parts[2])).digest()).decode().rstrip("=")
        fingerprints.append("SHA256:" + digest)
    return {"keys": "\n".join(lines) + "\n", "fingerprints": sorted(set(fingerprints))}


def trust(connection, observed):
    path = known_hosts(validate_connection(connection))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(observed["keys"], encoding="utf-8")


def agent_command(connection):
    entry = connection.get("entry", "").strip()
    if connection["platform"] == "windows":
        target = ("'" + entry.replace("'", "''") + "'") if entry else "(Join-Path $env:USERPROFILE '.codex-project-sync\\program\\CodexProjectSync.exe')"
        code = "[Console]::OutputEncoding=[Text.UTF8Encoding]::new();$env:PYTHONUTF8='1';& " + target + " --agent"
        return "powershell -NoProfile -NonInteractive -EncodedCommand " + base64.b64encode(code.encode("utf-16le")).decode()
    target = shlex.quote(entry) if entry else '"$HOME/Applications/CodexProjectSync.app/Contents/Resources/backend/CodexProjectSync"'
    return target + " --agent"


def rpc(connection, operation, **arguments):
    connection = validate_connection(connection)
    request = {"protocol": 1, "op": operation, **arguments}
    if connection["auth"] == "password":
        output = password_rpc(connection, request)
    else:
        output = core.run(["ssh", *options(connection), connection["user"] + "@" + connection["address"], agent_command(connection)],
                          input=json.dumps(request, ensure_ascii=False), timeout=900)
    try:
        response = json.loads(output)
    except ValueError as error:
        raise RuntimeError("目标接收组件没有返回有效结果，请核对安装位置和系统选择") from error
    if not response.get("ok"):
        raise RuntimeError(response.get("error", "远程操作失败"))
    return response["result"]


def transfer(connection, direction, local, remote, progress, resume=False):
    connection = validate_connection(connection)
    local = pathlib.Path(local)
    local.parent.mkdir(parents=True, exist_ok=True)
    remote = remote.replace("\\", "/")
    if connection["auth"] == "password":
        password_transfer(connection, direction, local, remote, progress, resume)
        return
    if connection["platform"] == "windows" and re.match(r"^[A-Za-z]:/", remote):
        remote = "/" + remote
    def quote(value):
        if "\n" in value or "\r" in value or '"' in value:
            raise RuntimeError("传输路径包含不支持的字符")
        return '"' + value + '"'
    verb = ("reget" if resume else "get") if direction == "get" else ("reput" if resume else "put")
    source, target = (remote, str(local).replace("\\", "/")) if direction == "get" else (str(local).replace("\\", "/"), remote)
    command = verb + " " + quote(source) + " " + quote(target)
    host = connection["address"]
    if ":" in host:
        host = "[" + host + "]"
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(["sftp", "-b", "-", *options(connection, True), connection["user"] + "@" + host],
                                   stdin=subprocess.PIPE, stdout=errors, stderr=errors,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        process.stdin.write((command + "\n").encode("utf-8"))
        process.stdin.close()
        started = time.monotonic()
        while process.poll() is None:
            size = local.stat().st_size if local.exists() else 0
            progress("正在传输更新包 · " + str(round(size / 1048576, 1)) + " MiB · 已用 " + str(int(time.monotonic() - started)) + " 秒")
            time.sleep(0.5)
        if process.returncode:
            errors.seek(0)
            raise RuntimeError("文件传输失败，已保留断点：" + errors.read().decode("utf-8", errors="replace")[-1500:])


def discover(mode):
    if mode == "tailscale":
        executable = shutil.which("tailscale")
        if not executable and os.name != "nt":
            candidate = pathlib.Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")
            executable = str(candidate) if candidate.exists() else None
        if not executable:
            return []
        data = json.loads(core.run([executable, "status", "--json"], timeout=12))
        return [{"name": device.get("HostName") or device.get("DNSName"), "address": address, "online": device.get("Online", False)}
                for device in data.get("Peer", {}).values() for address in device.get("TailscaleIPs", []) if ":" not in address]
    if os.name == "nt":
        code = "@(Get-NetNeighbor -AddressFamily IPv4 | Where-Object {$_.State -notin @('Unreachable','Incomplete')} | Select-Object -ExpandProperty IPAddress) | ConvertTo-Json -Compress"
        data = json.loads(core.run(["powershell", "-NoProfile", "-Command", code]) or "[]")
        addresses = [data] if isinstance(data, str) else data
    else:
        addresses = re.findall(r"\((\d+\.\d+\.\d+\.\d+)\)", core.run(["arp", "-an"]))
    return [{"name": address, "address": address, "online": None} for address in sorted(set(addresses))
            if ipaddress.ip_address(address).is_private and not address.endswith(".255")]
