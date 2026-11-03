"""Connect raw PCM streams to the selected phone's SCO ports."""
import asyncio
import contextlib
import json
import re
import uuid

from server.common.contracts import GatewayError, address


class PipeWireAudio:
    def __init__(self, requested, spawn=asyncio.create_subprocess_exec):
        self.address = address(requested).replace(":", "_")
        self.spawn = spawn
        self.children = []
        self.capture = self.playback = None
        tag = uuid.uuid4().hex
        self.rx, self.tx = "phone-gateway-rx-" + tag, "phone-gateway-tx-" + tag
        self.links = []

    async def child(self, *args, **kwargs):
        # Cancellation during spawn must not leave an untracked child.
        task = asyncio.create_task(self.spawn(*args, **kwargs))
        try:
            proc = await asyncio.shield(task)
        except asyncio.CancelledError:
            proc = await task
            self.children.append(proc)
            raise
        self.children.append(proc)
        return proc

    async def command(self, *args):
        proc = await self.child(*args, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        try:
            output, _ = await asyncio.wait_for(proc.communicate(), 3)
            self.children.remove(proc)
            if proc.returncode != 0:
                raise GatewayError("pipewire_command_failed", 503)
            return output.decode("utf-8", errors="replace")
        except asyncio.TimeoutError:
            raise GatewayError("pipewire_command_timeout", 503) from None

    async def start(self):
        try:
            # Check each installed tool once. Older verified tools have no --raw.
            record_help, play_help = await asyncio.gather(
                self.command("pw-record", "--help"), self.command("pw-play", "--help"))
            raw_pattern = r"(?<![\w-])--raw(?=[\s,=]|$)"
            record_raw = ["--raw"] if re.search(raw_pattern, record_help) else []
            play_raw = ["--raw"] if re.search(raw_pattern, play_help) else []
            self.capture = await self.child("pw-record", "--target", "0", "--rate", "24000",
                "--channels", "1", "--format", "s16", "--latency", "20ms", *record_raw, "-P", json.dumps({"node.name": self.rx}), "-",
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            self.playback = await self.child("pw-play", "--target", "0", "--rate", "24000",
                "--channels", "1", "--format", "s16", "--latency", "20ms", *play_raw, "-P", json.dumps({"node.name": self.tx}), "-",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            if hasattr(self.playback.stdin, "transport"):
                self.playback.stdin.transport.set_write_buffer_limits(high=960, low=480)
            # Discard pre-link capture. It cannot be accepted as phone audio.
            drain = asyncio.create_task(self._discard())
            try:
                while True:
                    self.check_children()
                    outputs, inputs = await asyncio.gather(self.command("pw-link", "-o"), self.command("pw-link", "-i"))
                    sources = [p for p in outputs.splitlines() if p.startswith("bluez_input." + self.address + ".") and p.endswith(":output_MONO")]
                    sinks = [p for p in inputs.splitlines() if p.startswith("bluez_output." + self.address + ".") and p.endswith(":input_MONO")]
                    if len(sources) > 1 or len(sinks) > 1:
                        raise GatewayError("ambiguous_sco_ports", 409)
                    if sources and sinks and self.rx + ":input_MONO" in inputs.splitlines() and self.tx + ":output_MONO" in outputs.splitlines():
                        self.links = [(sources[0], self.rx + ":input_MONO"), (self.tx + ":output_MONO", sinks[0])]
                        break
                    await asyncio.sleep(0.1)
            finally:
                drain.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await drain
            for source, sink in self.links:
                await self.command("pw-link", source, sink)
        except FileNotFoundError:
            raise GatewayError("audio_tools_unavailable", 503) from None

    async def _discard(self):
        while await self.capture.stdout.read(4096):
            pass

    def check_children(self):
        if self.capture.returncode is not None or self.playback.returncode is not None:
            raise GatewayError("audio_process_closed", 503)

    async def read(self):
        # read() can split a PCM sample. readexactly preserves the frame contract.
        try:
            return await self.capture.stdout.readexactly(960)
        except asyncio.IncompleteReadError:
            raise GatewayError("audio_process_closed", 503) from None

    async def write(self, data):
        self.check_children()
        self.playback.stdin.write(data)
        try:
            await asyncio.wait_for(self.playback.stdin.drain(), 0.2)
        except (asyncio.TimeoutError, ConnectionError, BrokenPipeError):
            raise GatewayError("audio_backpressure", 409) from None

    async def monitor(self):
        while True:
            await asyncio.sleep(0.5)
            self.check_children()
            outputs, inputs = await asyncio.gather(self.command("pw-link", "-o"), self.command("pw-link", "-i"))
            if not all(source in outputs.splitlines() and sink in inputs.splitlines() for source, sink in self.links):
                raise GatewayError("sco_closed", 409)

    async def close(self):
        # Child termination removes our private nodes and their links.
        for proc in self.children:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
        for proc in self.children:
            if proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), 2)
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    await proc.wait()
        self.children.clear()
