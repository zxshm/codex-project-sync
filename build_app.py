"""构建当前平台的独立运行包，并安装专用桌面入口。"""
import os
import pathlib
import plistlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent


def run(arguments):
    subprocess.run([str(value) for value in arguments], cwd=ROOT, check=True)


def main():
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--name", "CodexProjectSync",
         "--add-data", "界面.html:.", "--collect-all", "zstandard", "main.py"])
    distribution = ROOT / "dist/CodexProjectSync"
    if os.name == "nt":
        target = pathlib.Path.home() / ".codex-project-sync/program"
        if target.exists():
            if target.is_symlink() or target.resolve().parent != (pathlib.Path.home() / ".codex-project-sync").resolve():
                raise RuntimeError("已有程序目录不符合安装范围")
            shutil.rmtree(target)
        shutil.copytree(distribution, target)
        launcher = target / "启动项目同步.vbs"
        command = '"' + str(target / "CodexProjectSync.exe") + '"'
        launcher.write_text('Set shell = CreateObject("WScript.Shell")\n'
                            'shell.Run "' + command.replace('"', '""') + '", 0, False\n', encoding="utf-16")
        def quote(value):
            return "'" + str(value).replace("'", "''") + "'"
        script = "$desktop=[Environment]::GetFolderPath('Desktop');$link=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $desktop 'Codex项目同步.lnk'));"
        script += "$link.TargetPath='C:\\Windows\\System32\\wscript.exe';$link.Arguments=" + quote('"' + str(launcher) + '"') + ";"
        script += "$link.WorkingDirectory=" + quote(target) + ";$link.Description='手动选择设备与项目进行独立同步';$link.IconLocation='C:\\Windows\\System32\\imageres.dll,68';$link.Save()"
        run(["powershell", "-NoProfile", "-Command", script])
        print("已安装 " + str(target))
    else:
        target = pathlib.Path.home() / "Applications/CodexProjectSync.app"
        if target.exists():
            if target.is_symlink() or target.resolve().parent != (pathlib.Path.home() / "Applications").resolve():
                raise RuntimeError("已有应用目录不符合安装范围")
            shutil.rmtree(target)
        contents = target / "Contents"
        (contents / "MacOS").mkdir(parents=True)
        shutil.copytree(distribution, contents / "Resources/backend")
        info = {"CFBundleName": "Codex项目同步", "CFBundleDisplayName": "Codex项目同步", "CFBundleExecutable": "CodexProjectSync",
                "CFBundleIdentifier": "local.codex-project-sync", "CFBundlePackageType": "APPL", "CFBundleVersion": "1",
                "CFBundleShortVersionString": "1.0.1", "LSUIElement": True,
                "NSDocumentsFolderUsageDescription": "访问用户选择的项目目录以迁移文件和聊天。"}
        (contents / "Info.plist").write_bytes(plistlib.dumps(info))
        run(["swiftc", ROOT / "mac_launcher.swift", "-o", contents / "MacOS/CodexProjectSync"])
        run(["codesign", "--force", "--deep", "--sign", "-", target])
        desktop = pathlib.Path.home() / "Desktop/Codex项目同步.app"
        if desktop.exists() or desktop.is_symlink():
            if desktop.is_symlink() and desktop.resolve() == target.resolve():
                desktop.unlink()
            else:
                raise RuntimeError("桌面已有同名项目，请先核对")
        desktop.symlink_to(target)
        print("已安装 " + str(target))


if __name__ == "__main__":
    main()
