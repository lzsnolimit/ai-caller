"""Mac service entry, launched by the Bluetooth power-owner wrapper."""
import argparse
import asyncio
import logging
import signal

from aiohttp import web
from server.common.api import create_app
from .service import MacService


async def serve(port):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    adapter = MacService()
    adapter.mac_only = True
    app = create_app(adapter, adapter.audio, managed=True)
    app["conversations"].base = f"http://127.0.0.1:{port}"
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=5)
    try:
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        print('{"stage":"mac-service-ready"}', flush=True)
        await stop.wait()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    asyncio.run(serve(args.port))
