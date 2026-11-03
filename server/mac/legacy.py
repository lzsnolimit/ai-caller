"""Run the existing gateway API over a selected Mac built-in Bluetooth phone."""
import argparse
import asyncio
import logging
import signal

from aiohttp import web

from .native.acipc_pair import run
from .native.acipc_stack import emit
from server.common.api import create_app
from .adapter import MacPhoneAdapter, MacSCOAudio
from server.common.contracts import address


async def main(args):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    async def serve(device, connection, protocol, transport):
        adapter = MacPhoneAdapter(args.address, device, connection, protocol, transport)
        app = create_app(adapter, lambda selected: MacSCOAudio(selected, adapter))
        runner = web.AppRunner(app, access_log=None)
        try:
            await runner.setup()
            await web.TCPSite(runner, args.host, args.port).start()
            emit('mac-gateway-ready', port=args.port, model_logic_reused=True)
            await transport.guarded(stop.wait(), float('inf'))
        finally:
            if app['state'].audio_ws:
                await app['state'].clear_lease()
            await runner.cleanup()
            emit('mac-gateway-closed', rx_packets=adapter.rx_packets,
                 tx_packets=adapter.tx_packets, bad_packets=adapter.bad_packets)
    try:
        await run(args.address, serve)
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser()
    parser.add_argument('--address', required=True, type=address)
    parser.add_argument('--host', default='127.0.0.1', choices=['127.0.0.1'])
    parser.add_argument('--port', type=int, default=18765)
    args = parser.parse_args()
    try:
        asyncio.run(main(args))
    except BaseException as error:
        emit('mac-gateway-failed', error_type=type(error).__name__)
        raise SystemExit(1)
