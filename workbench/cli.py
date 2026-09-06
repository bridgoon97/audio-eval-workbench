"""跨平台启动入口；默认只监听本机。"""

import argparse
import socket
import sys
import threading
import webbrowser
from pathlib import Path

import uvicorn

from .app import create_app


def main():
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
    app = create_app(
        args.data,
        root / "dist",
        args.secure_cookie or bool(args.public_origin),
        args.public_origin,
    )
    if not (root / "dist" / "index.html").exists():
        print("尚未构建界面；开发时请运行 npm run build，或使用 Vite 开发服务。")
    print(f"\n听鉴 · 数据目录：{args.data.resolve()}")
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
    if not args.no_browser:
        threading.Timer(
            1.5, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}")
        ).start()
    uvicorn.run(
        app,
        host="0.0.0.0" if args.lan else "127.0.0.1",
        port=args.port,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
