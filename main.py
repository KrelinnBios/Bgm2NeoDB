import argparse
import socket
import sys
import threading
import time
import webbrowser

from app.config import HOST, ORIGIN, PORT


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Bgm2NeoDB 本地收藏迁移工具")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    args = parser.parse_args()
    try:
        import uvicorn

        from app.routes import create_app
    except ImportError:
        print("请先安装依赖：python -m pip install -r requirements.txt")
        return 1
    # Bind before opening the browser; never navigate to a different process on an occupied port.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((HOST, PORT))
        sock.listen(128)
    except OSError:
        sock.close()
        print(
            "端口 8765 已被占用。如果程序已启动，请打开 http://127.0.0.1:8765；否则请关闭占用端口的程序。"
        )
        return 1
    server = uvicorn.Server(
        uvicorn.Config(create_app(), host=HOST, port=PORT, access_log=False, log_level="warning")
    )

    def open_when_ready():
        for _ in range(100):
            if server.started:
                webbrowser.open(ORIGIN)
                return
            time.sleep(0.1)

    if not args.no_browser:
        threading.Thread(target=open_when_ready, daemon=True).start()
    print(f"Bgm2NeoDB 已启动：{ORIGIN}\n按 Ctrl+C 停止。迁移记录会保存在本机。", flush=True)
    try:
        server.run(sockets=[sock])
    finally:
        sock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
