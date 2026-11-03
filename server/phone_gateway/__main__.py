import argparse

from aiohttp import web

from .adapter import DBusPhoneAdapter
from .api import create_app


def main():
    parser = argparse.ArgumentParser(description="Run the local phone gateway")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    app = create_app(DBusPhoneAdapter())
    # Access logs contain addresses and query values. Disable them.
    web.run_app(app, host=args.host, port=args.port, access_log=None, print=None)


if __name__ == "__main__":
    main()
