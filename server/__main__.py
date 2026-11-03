"""One startup command, selected by the host OS."""
import argparse
import os
from pathlib import Path
import platform
import sys


def main():
    parser = argparse.ArgumentParser(description="Phone gateway shared service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    system = platform.system()
    if system == "Darwin":
        if args.host != "127.0.0.1":
            parser.error("Mac v1 binds to 127.0.0.1 only")
        root = Path(__file__).resolve().parent
        env = {**os.environ, "PHONE_MAC_PYTHON": sys.executable}
        os.execvpe("node", ["node", str(root / "run-mac.cjs"), "-", str(args.port)], env)
    elif system == "Linux":
        from aiohttp import web
        from .common.api import create_app
        from .pi.adapter import DBusPhoneAdapter
        app = create_app(DBusPhoneAdapter(), managed=True)
        host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
        app["conversations"].base = f"http://{host}:{args.port}"
        web.run_app(app, host=args.host, port=args.port, access_log=None, print=None)
    else:
        parser.error("This server supports Linux and macOS only; Windows HFP is not implemented")


if __name__ == "__main__":
    main()
