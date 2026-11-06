"""程序入口，同时支持桌面界面和 SSH 接收组件。"""
import json
import sys


def main():
    for stream in [sys.stdin, sys.stdout, sys.stderr]:
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    if "--agent" in sys.argv:
        import project_sync
        try:
            result = project_sync.dispatch(json.load(sys.stdin))
            print(json.dumps({"ok": True, "result": result}, ensure_ascii=False), flush=True)
        except Exception as error:
            print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), flush=True)
        return
    import server
    server.serve(open_browser="--headless" not in sys.argv)


if __name__ == "__main__":
    main()
