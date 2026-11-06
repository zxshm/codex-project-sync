"""调用 Windows Codex 自身的退出快捷键，并等待所属后端结束。"""
import json
import time


def owned_processes(records):
    """按安装路径确认 Codex 桌面进程，排除独立运行的命令行后端。"""
    desktop = {row["ProcessId"] for row in records
               if row["Name"].lower() == "chatgpt.exe"
               and "/openai.codex_" in (row.get("ExecutablePath") or "").replace("\\", "/").lower()}
    owned = set(desktop)
    for row in records:
        if row["Name"].lower() == "codex.exe" and row["ParentProcessId"] in desktop:
            owned.add(row["ProcessId"])
    return desktop, owned


def request_quit(cfg):
    """确认目标窗口获得焦点后调用 Ctrl+Q，应用或后端未退出时停止。"""
    import ctypes as ct
    from ctypes import wintypes as w
    import sync_core as core

    started = time.monotonic()
    command = ("[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
               "@(Get-CimInstance Win32_Process | Where-Object {$_.Name -in @('ChatGPT.exe','codex.exe')} | "
               "Select-Object Name,ProcessId,ParentProcessId,ExecutablePath) | ConvertTo-Json -Compress")
    records = json.loads(core.run(["powershell", "-NoProfile", "-Command", command]) or "[]")
    if isinstance(records, dict):
        records = [records]
    desktop, owned = owned_processes(records)
    if not desktop:
        raise RuntimeError("未找到已核验的 Codex 桌面进程，停止退出操作")

    user = ct.WinDLL("user32", use_last_error=True)
    kernel = ct.WinDLL("kernel32", use_last_error=True)
    callback_type = ct.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    user.EnumWindows.argtypes = [callback_type, w.LPARAM]
    user.GetWindowThreadProcessId.argtypes = [w.HWND, ct.POINTER(w.DWORD)]
    user.GetWindowThreadProcessId.restype = w.DWORD
    user.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ct.c_int]
    user.IsWindowVisible.argtypes = [w.HWND]
    user.GetForegroundWindow.restype = w.HWND
    user.ShowWindowAsync.argtypes = [w.HWND, ct.c_int]
    user.SetForegroundWindow.argtypes = [w.HWND]
    user.AttachThreadInput.argtypes = [w.DWORD, w.DWORD, w.BOOL]
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    kernel.WaitForSingleObject.restype = w.DWORD
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
    handles = []
    backend_handles = []
    try:
        for pid in sorted(owned):
            handle = kernel.OpenProcess(0x00100000 | (1 if pid not in desktop else 0), False, pid)
            if not handle:
                if ct.get_last_error() == 87:
                    continue
                raise RuntimeError("无法确认 Codex 进程退出状态，停止安装")
            handles.append(handle)
            if pid not in desktop:
                backend_handles.append((pid, handle))

        def main_windows():
            found = []
            @callback_type
            def visit(hwnd, _):
                pid = w.DWORD()
                user.GetWindowThreadProcessId(hwnd, ct.byref(pid))
                name = ct.create_unicode_buffer(128)
                user.GetClassNameW(hwnd, name, len(name))
                if pid.value in desktop and name.value == "Chrome_WidgetWin_1":
                    found.append((bool(user.IsWindowVisible(hwnd)), hwnd))
                return True
            user.EnumWindows(visit, 0)
            return sorted(found, reverse=True)

        windows = main_windows()
        if not windows:
            core.reopen(cfg)
            for _ in range(30):
                time.sleep(0.2)
                windows = main_windows()
                if windows:
                    break
        if not windows:
            raise RuntimeError("无法找到 Codex 主窗口，未发送退出快捷键")
        hwnd = windows[0][1]
        # 建立当前工作线程的消息队列，确保可以临时连接前台线程的输入队列。
        message = w.MSG()
        user.PeekMessageW.argtypes = [ct.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT]
        user.PeekMessageW(ct.byref(message), None, 0, 0, 0)
        foreground = user.GetForegroundWindow()
        current_thread = kernel.GetCurrentThreadId()
        threads = {user.GetWindowThreadProcessId(hwnd, None)}
        if foreground:
            threads.add(user.GetWindowThreadProcessId(foreground, None))
        attached = []
        try:
            for thread in threads - {current_thread}:
                if user.AttachThreadInput(current_thread, thread, True):
                    attached.append(thread)
            user.ShowWindowAsync(hwnd, 9)
            user.SetForegroundWindow(hwnd)
        finally:
            for thread in attached:
                user.AttachThreadInput(current_thread, thread, False)
        time.sleep(0.2)
        if user.GetForegroundWindow() != hwnd:
            raise RuntimeError("Codex 主窗口未获得焦点，未发送退出快捷键")

        class Keyboard(ct.Structure):
            _fields_ = [("key", w.WORD), ("scan", w.WORD), ("flags", w.DWORD),
                        ("time", w.DWORD), ("extra", ct.c_size_t)]

        class Mouse(ct.Structure):
            _fields_ = [("x", w.LONG), ("y", w.LONG), ("data", w.DWORD),
                        ("flags", w.DWORD), ("time", w.DWORD), ("extra", ct.c_size_t)]

        class Payload(ct.Union):
            _fields_ = [("keyboard", Keyboard), ("mouse", Mouse)]

        class Input(ct.Structure):
            _fields_ = [("kind", w.DWORD), ("payload", Payload)]

        inputs = (Input * 4)()
        for event, (key, flags) in zip(inputs, [(0x11, 0), (0x51, 0), (0x51, 2), (0x11, 2)]):
            event.kind = 1
            event.payload.keyboard = Keyboard(key, 0, flags, 0, 0)
        user.SendInput.argtypes = [w.UINT, ct.POINTER(Input), ct.c_int]
        user.SendInput.restype = w.UINT
        if user.SendInput(4, inputs, ct.sizeof(Input)) != 4:
            release = (Input * 2)(inputs[2], inputs[3])
            user.SendInput(2, release, ct.sizeof(Input))
            raise RuntimeError("Codex 退出快捷键未完整发送，停止安装")
        deadline = time.monotonic() + 25
        for handle in [handle for handle in handles if handle not in {item[1] for item in backend_handles}]:
            remaining = max(0, int((deadline - time.monotonic()) * 1000))
            if kernel.WaitForSingleObject(handle, remaining) != 0:
                raise RuntimeError("Codex 桌面或托盘尚未退出，未写入聊天数据库")
        stopped_orphans = []
        for pid, handle in backend_handles:
            status = kernel.WaitForSingleObject(handle, 3000)
            if status == 258:
                # 桌面已退出，处理退出前已确认归属且当时空闲的遗留后端。
                if not kernel.TerminateProcess(handle, 0) or kernel.WaitForSingleObject(handle, 5000) != 0:
                    raise RuntimeError("所属遗留后端无法结束，停止数据库写入")
                stopped_orphans.append(pid)
            elif status != 0:
                raise RuntimeError("无法核验所属后端状态，停止数据库写入")
        return {"方式": "Ctrl+Q", "桌面进程": sorted(desktop), "已退出进程": sorted(owned),
                "全部结束": True, "已结束孤立后端": stopped_orphans, "耗时秒": round(time.monotonic() - started, 3)}
    finally:
        for handle in handles:
            kernel.CloseHandle(handle)
