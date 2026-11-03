"""Selected-phone Classic authentication and HFP SLC; no calls or audio yet."""
import argparse
import asyncio
import ctypes
import logging
import sys

from bumble import core, hci, hfp, pairing, rfcomm, sdp
from bumble.device import Device, DeviceConfiguration
from bumble.keys import KeyStore, PairingKeys
from ...common.storage import Store
from .acipc_stack import ROOT, NativeTransport, NoFirmwareHost, NO_PAIR_COMMANDS, NO_PAIR_EVENTS, emit

ADDRESS_COMMANDS = {
    hci.HCI_LINK_KEY_REQUEST_REPLY_COMMAND,
    hci.HCI_LINK_KEY_REQUEST_NEGATIVE_REPLY_COMMAND,
    hci.HCI_IO_CAPABILITY_REQUEST_REPLY_COMMAND,
    hci.HCI_IO_CAPABILITY_REQUEST_NEGATIVE_REPLY_COMMAND,
    hci.HCI_USER_CONFIRMATION_REQUEST_REPLY_COMMAND,
    hci.HCI_USER_CONFIRMATION_REQUEST_NEGATIVE_REPLY_COMMAND,
}
HANDLE_COMMANDS = {hci.HCI_AUTHENTICATION_REQUESTED_COMMAND, hci.HCI_SET_CONNECTION_ENCRYPTION_COMMAND}
ADDRESS_EVENTS = {
    hci.HCI_LINK_KEY_REQUEST_EVENT, hci.HCI_LINK_KEY_NOTIFICATION_EVENT,
    hci.HCI_IO_CAPABILITY_REQUEST_EVENT, hci.HCI_IO_CAPABILITY_RESPONSE_EVENT,
    hci.HCI_USER_CONFIRMATION_REQUEST_EVENT, hci.HCI_SIMPLE_PAIRING_COMPLETE_EVENT,
}
HANDLE_EVENTS = {hci.HCI_AUTHENTICATION_COMPLETE_EVENT, hci.HCI_ENCRYPTION_CHANGE_EVENT,
                 hci.HCI_ENCRYPTION_CHANGE_V2_EVENT}


class SelectedTransport(NativeTransport):
    def __init__(self, target):
        super().__init__()
        self.target = target
        self.handle = None

    def on_packet(self, packet):
        raw = bytes(packet)
        if not raw or raw[0] not in (1, 2, 3):
            raise RuntimeError("unsupported outbound packet type")
        if raw[0] == 1:
            opcode = int.from_bytes(raw[1:3], 'little')
            if (opcode >> 10) == 0x3F:
                raise RuntimeError("vendor command prohibited")
            if opcode in NO_PAIR_COMMANDS:
                if opcode in ADDRESS_COMMANDS and raw[4:10] == bytes(self.target):
                    pass
                elif opcode in HANDLE_COMMANDS and self.handle is not None and int.from_bytes(raw[4:6], 'little') == self.handle:
                    pass
                else:
                    raise RuntimeError("authentication command outside selected-phone scope")
        payload = ctypes.create_string_buffer(raw[1:])
        status = self.lib.acipc_send(raw[0], payload, len(raw) - 1)
        if status:
            self.failure = RuntimeError(f"native send failed: {status}")
            raise self.failure
        self.sent += 1

    async def pump(self):
        try:
            while self.running:
                packet = await asyncio.to_thread(self.read_one)
                if packet is None:
                    continue
                if packet[0] == 4 and packet[1] in NO_PAIR_EVENTS:
                    event = packet[1]
                    if event in ADDRESS_EVENTS:
                        start = 4 if event == hci.HCI_SIMPLE_PAIRING_COMPLETE_EVENT else 3
                        valid = packet[start:start+6] == bytes(self.target)
                    elif event in HANDLE_EVENTS:
                        valid = self.handle is not None and int.from_bytes(packet[4:6], 'little') == self.handle
                    else:
                        valid = False
                    if not valid:
                        raise RuntimeError("pairing event outside selected-phone scope")
                if packet[0] == 4 and packet[1] == hci.HCI_DISCONNECTION_COMPLETE_EVENT:
                    emit("controller-link-disconnected", status=packet[3], hci_reason=packet[6])
                if packet[0] == 4 and packet[1] in (hci.HCI_AUTHENTICATION_COMPLETE_EVENT, hci.HCI_SIMPLE_PAIRING_COMPLETE_EVENT):
                    emit('authentication-status', event=packet[1], status=packet[3])
                self.received += 1
                if self.sink:
                    self.sink.on_packet(packet)
        except Exception as error:
            self.failure = error
            self.running = False


class SelectedKeyStore(KeyStore):
    """Pairing keys in a private local file: <data dir>/devices/pair-keys.json (mode 600)."""

    def __init__(self, controller, target, store=None):
        self.target = str(target)
        self.account = f'{controller}/{self.target}'
        self.store = store or Store()
        self.failure = None

    def entries(self):
        return self.store.load('devices', 'pair-keys', {})

    async def delete(self):
        entries = self.entries()
        if entries.pop(self.account, None) is not None:
            self.store.save('devices', 'pair-keys', entries)

    async def get(self, name):
        if name != self.target:
            return None
        entry = self.entries().get(self.account)
        if not entry:
            return None
        key_type, value = entry['type'], bytes.fromhex(entry['key'])
        if key_type not in (5, 8) or len(value) != 16:
            raise RuntimeError('invalid stored pairing key')
        return PairingKeys(link_key=PairingKeys.Key(value, True), link_key_type=key_type)

    async def update(self, name, keys):
        try:
            if name != self.target or not keys.link_key or not keys.link_key.authenticated:
                raise RuntimeError('refuse unverified or out-of-scope pairing key')
            if keys.link_key_type not in (5, 8) or len(keys.link_key.value) != 16:
                raise RuntimeError('invalid authenticated Classic key')
            entries = self.entries()
            entries[self.account] = {'type': keys.link_key_type, 'key': bytes(keys.link_key.value).hex()}
            self.store.save('devices', 'pair-keys', entries)
            emit('authenticated-key-saved', storage='local-file', payload_logged=False)
        except Exception as error:
            self.failure = error
            raise


class NumericDelegate(pairing.PairingDelegate):
    def __init__(self):
        super().__init__(io_capability=self.DISPLAY_OUTPUT_AND_YES_NO_INPUT)

    async def confirm(self, auto=False):
        return False

    async def compare_numbers(self, number, digits):
        started = asyncio.get_running_loop().time()
        emit('numeric-comparison-required', digits=f'{number:0{digits}d}')
        # The operator must compare this code with the selected phone's dialog.
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        pipe, _ = await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin)
        try:
            line = await asyncio.wait_for(reader.readline(), 40)
            confirmed = line.decode().strip() == f'{number:0{digits}d}'
            emit('numeric-comparison-result', matched=confirmed,
                 elapsed_seconds=round(asyncio.get_running_loop().time() - started, 2))
            return confirmed
        finally:
            pipe.close()


async def serve_hfp(protocol, operation):
    """Consume unsolicited notifications for the entire service callback lifetime."""
    consumer = asyncio.create_task(protocol.run())
    service = asyncio.create_task(operation)
    try:
        done, _ = await asyncio.wait((consumer, service), return_when=asyncio.FIRST_COMPLETED)
        if service in done:
            return await service
        await consumer
        # A closed control channel must release the service's connected state.
        return None
    finally:
        service.cancel()
        try:
            await service
        except asyncio.CancelledError:
            pass
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)


async def run(target, on_ready=None, delegate=None):
    address = hci.Address(target, hci.Address.PUBLIC_DEVICE_ADDRESS)
    transport = SelectedTransport(address)
    host = NoFirmwareHost(controller_source=transport, controller_sink=transport)
    config = DeviceConfiguration(name='AI-Phone', le_enabled=False,
        classic_enabled=True, classic_smp_enabled=False, classic_accept_any=False,
        connectable=False, discoverable=False, keystore=None)
    device = Device(config=config, host=host)
    device.command_timeout = 3
    delegate = delegate or NumericDelegate()
    device.pairing_config_factory = lambda connection: pairing.PairingConfig(
        sc=True, mitm=True, bonding=True, delegate=delegate)
    connection = None
    client = None
    rfcomm_client = None
    primary = None
    acl_closed = asyncio.Event()
    await transport.start()
    try:
        await transport.guarded(device.power_on(), 15)
        device.keystore = SelectedKeyStore(device.public_address, address)
        emit('independent-stack-ready', classic=True, firmware_driver=False)
        connection = await transport.guarded(device.connect_classic(address, timeout=8), 10)
        connection.on(connection.EVENT_DISCONNECTION, lambda *_: acl_closed.set())
        transport.handle = connection.handle
        emit('phone-acl-connected')
        await transport.guarded(connection.authenticate(), 50)
        await transport.guarded(connection.encrypt(), 5)
        # Key updates are scheduled by Device; check the private store before accepting success.
        await asyncio.sleep(0.1)
        if device.keystore.failure:
            raise device.keystore.failure
        keys = await device.keystore.get(str(address))
        if not keys or not keys.link_key.authenticated:
            raise RuntimeError('authenticated persistent key missing')
        emit('phone-authenticated', encrypted=connection.is_encrypted, authenticated_key=True)
        client = sdp.Client(connection)
        await transport.guarded(client.connect(), 5)
        records = await transport.guarded(client.search_attributes(
            [core.UUID.from_16_bits(0x111F)], [sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID]), 5)
        channels = set()
        for record in records:
            for attribute in record:
                for descriptor in attribute.value.value:
                    elements = descriptor.value
                    if len(elements) >= 2 and elements[0].value == core.BT_RFCOMM_PROTOCOL_ID:
                        channels.add(elements[1].value)
        if len(channels) != 1:
            raise RuntimeError('missing or ambiguous HFP gateway channel')
        channel = channels.pop()
        if not isinstance(channel, int) or not 1 <= channel <= 30:
            raise RuntimeError('invalid HFP gateway channel')
        await transport.guarded(client.disconnect(), 2)
        rfcomm_client = rfcomm.Client(connection)
        mux = await transport.guarded(rfcomm_client.start(), 5)
        dlc = await transport.guarded(mux.open_dlc(channel), 5)
        protocol = hfp.HfProtocol(dlc, hfp.HfConfiguration([], [], [hfp.AudioCodec.CVSD]))
        await transport.guarded(protocol.initiate_slc(), 8)
        emit('hfp-control-ready', audio_started=False, call_started=False)
        if on_ready:
            await serve_hfp(protocol, on_ready(device, connection, protocol, transport))
    except BaseException as error:
        primary = error
        raise
    finally:
        cleanup = None
        for operation in (
            rfcomm_client.shutdown if rfcomm_client and rfcomm_client.multiplexer else None,
            client.disconnect if client and client.channel else None,
            connection.disconnect if connection else None,
        ):
            if operation and not transport.failure and not acl_closed.is_set():
                try:
                    await transport.guarded(operation(), 3)
                except BaseException as error:
                    cleanup = error
                    emit('cleanup-failed', error_type=type(error).__name__)
        try:
            await transport.stop()
        except BaseException:
            if primary is None:
                raise
        if cleanup and primary is None:
            raise RuntimeError('connection cleanup failed') from cleanup


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser()
    parser.add_argument('--target', required=True)
    args = parser.parse_args()
    try:
        asyncio.run(asyncio.wait_for(run(args.target), 85))
    except BaseException as error:
        emit('probe-failed', error_type=type(error).__name__, hci_error=getattr(error, 'error_code', None))
        raise SystemExit(1)
