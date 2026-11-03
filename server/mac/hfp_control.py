"""Keep late AT terminal responses out of the next transaction."""
import asyncio
from bumble import hfp
from server.common.contracts import GatewayError


class StaleIdentity(Exception):
    """The guard rejected the command at the last step; nothing was sent."""


class HFPControl:
    RECOVERY_SECONDS = 3
    def __init__(self, protocol):
        self.protocol = protocol
        self.original = protocol.execute_command
        self.lock = asyncio.Lock()
        self.synchronized = True

    async def execute(self, command, *args, guard=None, **kwargs):
        async with self.lock:
            if not self.synchronized:
                raise GatewayError('hfp_control_unsynchronized', 503)
            # 最后一步再核对：锁内、写入前，没有其他 await。
            if guard is not None and not guard():
                raise StaleIdentity(command)
            try:
                return await self.original(command, *args, **kwargs)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self.synchronized = False
                # Hold the outer transaction lock while the original terminal is pending.
                # PCM and unsolicited phone notifications continue on separate paths.
                self.protocol.pending_command = command
                try:
                    async with asyncio.timeout(self.RECOVERY_SECONDS):
                        while True:
                            response = await self.protocol.response_queue.get()
                            if response.code in hfp.STATUS_CODES:
                                self.synchronized = True
                                break
                except asyncio.TimeoutError:
                    pass
                finally:
                    self.protocol.pending_command = None
                raise
