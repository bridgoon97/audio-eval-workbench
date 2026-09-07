"""跨平台启动入口；默认只监听本机。"""

import argparse
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn

from .app import create_app
from .version import VERSION


def main():
    # Windows 重定向输出可能采用 cp1252；中文启动提示必须可编码。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description="听鉴 · 音频算法评测工作台")
    parser.add_argument(
        "--data",
        type=Path,
        default=Path.home() / ".audio-eval-workbench",
        help="数据保存目录",
    )
    parser.add_argument("--lan", action="store_true", help="允许局域网访问")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument(
        "--secure-cookie", action="store_true", help="仅用于已配置 HTTPS 的反向代理部署"
    )
    parser.add_argument(
        "--public-origin",
        help="HTTPS 代理的确切外部源，例如 https://audio.example.internal",
    )
    args = parser.parse_args()
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    if args.public_origin:
        from urllib.parse import urlsplit

        parsed = urlsplit(args.public_origin)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or parsed.username
        ):
            parser.error("--public-origin 必须是无路径、无凭据的 HTTPS 源")
    host = "0.0.0.0" if args.lan else "127.0.0.1"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        if sys.platform == "win32":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((host, args.port))
            listener.listen(2048)
        except OSError as exc:
            message = f"听鉴 {VERSION} 未启动：无法占用端口 {args.port}。旧版服务或其他程序可能仍在运行。请先退出旧服务，再启动新版；不要只关闭浏览器。也请确认端口和监听权限。详细原因：{exc}"
            print(message, file=sys.stderr)
            if (
                sys.platform == "win32"
                and getattr(sys, "frozen", False)
                and not args.no_browser
            ):
                import ctypes

                ctypes.windll.user32.MessageBoxW(None, message, "听鉴启动失败", 0x10)
            raise SystemExit(1) from exc
        args.port = listener.getsockname()[1]
        serve(args, root, host, listener)


def serve(args, root, host, listener):
    app = create_app(
        args.data,
        root / "dist",
        args.secure_cookie or bool(args.public_origin),
        args.public_origin,
    )
    if not (root / "dist" / "index.html").exists():
        print("尚未构建界面；开发时请运行 npm run build，或使用 Vite 开发服务。")
    print(f"\n听鉴 {VERSION} · 数据目录：{args.data.resolve()}")
    print(f"本机地址：http://127.0.0.1:{args.port}")
    if args.lan:
        print("局域网模式已开启，请为所需网段配置防火墙入站规则。")
        for address in sorted(
            {
                x[4][0]
                for x in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
            }
        ):
            print(f"候选地址：http://{address}:{args.port}（请核对实际网卡）")
        print("HTTP 开发部署不加密流量；正式内网请按组织要求配置 HTTPS。")
    if app.state.setup_file.exists():
        print(f"首次初始化密钥：{app.state.setup_file.read_text().strip()}")
    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=args.port, proxy_headers=False)
    )

    def open_when_ready():
        while not server.started and not server.should_exit:
            time.sleep(0.1)
        if server.started and not server.should_exit:
            webbrowser.open(f"http://127.0.0.1:{args.port}")

    if not args.no_browser:
        threading.Thread(target=open_when_ready, daemon=True).start()
    try:
        server.run(sockets=[listener])
    finally:
        server.should_exit = True


if __name__ == "__main__":
    main()
