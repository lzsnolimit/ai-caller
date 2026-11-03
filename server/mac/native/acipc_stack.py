"""Bounded native-controller discovery/SDP probe; no pairing or calls."""
import argparse
import asyncio
import ctypes
import json
import logging
import os
from pathlib import Path

from bumble import core, hci, hfp, rfcomm, sdp
from bumble.device import Device, DeviceConfiguration
from bumble.host import Host
from bumble.keys import MemoryKeyStore

ROOT = Path(os.environ.get('PHONE_MAC_NATIVE_DIR', str(Path.home() / '.cache/phone-call-gateway/mac-native')))
NO_PAIR_COMMANDS = {
    hci.HCI_LINK_KEY_REQUEST_REPLY_COMMAND, hci.HCI_LINK_KEY_REQUEST_NEGATIVE_REPLY_COMMAND,
    hci.HCI_PIN_CODE_REQUEST_REPLY_COMMAND, hci.HCI_PIN_CODE_REQUEST_NEGATIVE_REPLY_COMMAND,
    hci.HCI_AUTHENTICATION_REQUESTED_COMMAND, hci.HCI_SET_CONNECTION_ENCRYPTION_COMMAND,
    hci.HCI_CHANGE_CONNECTION_LINK_KEY_COMMAND, hci.HCI_LINK_KEY_SELECTION_COMMAND,
    hci.HCI_IO_CAPABILITY_REQUEST_REPLY_COMMAND, hci.HCI_IO_CAPABILITY_REQUEST_NEGATIVE_REPLY_COMMAND,
    hci.HCI_USER_CONFIRMATION_REQUEST_REPLY_COMMAND, hci.HCI_USER_CONFIRMATION_REQUEST_NEGATIVE_REPLY_COMMAND,
    hci.HCI_USER_PASSKEY_REQUEST_REPLY_COMMAND, hci.HCI_USER_PASSKEY_REQUEST_NEGATIVE_REPLY_COMMAND,
    hci.HCI_REMOTE_OOB_DATA_REQUEST_REPLY_COMMAND, hci.HCI_REMOTE_OOB_DATA_REQUEST_NEGATIVE_REPLY_COMMAND,
    hci.HCI_REMOTE_OOB_EXTENDED_DATA_REQUEST_REPLY_COMMAND,
    hci.HCI_READ_STORED_LINK_KEY_COMMAND, hci.HCI_WRITE_STORED_LINK_KEY_COMMAND,
    hci.HCI_DELETE_STORED_LINK_KEY_COMMAND, hci.HCI_WRITE_AUTHENTICATION_ENABLE_COMMAND,
    hci.HCI_REFRESH_ENCRYPTION_KEY_COMMAND, hci.HCI_SEND_KEYPRESS_NOTIFICATION_COMMAND,
    hci.HCI_READ_LOCAL_OOB_DATA_COMMAND, hci.HCI_READ_LOCAL_OOB_EXTENDED_DATA_COMMAND,
    hci.HCI_WRITE_SIMPLE_PAIRING_DEBUG_MODE_COMMAND,
    hci.HCI_LE_ENABLE_ENCRYPTION_COMMAND, hci.HCI_LE_LONG_TERM_KEY_REQUEST_REPLY_COMMAND,
    hci.HCI_LE_LONG_TERM_KEY_REQUEST_NEGATIVE_REPLY_COMMAND,
    hci.HCI_LE_READ_LOCAL_P_256_PUBLIC_KEY_COMMAND, hci.HCI_LE_GENERATE_DHKEY_COMMAND,
    hci.HCI_LE_GENERATE_DHKEY_V2_COMMAND,
}
NO_PAIR_EVENTS = {
    hci.HCI_AUTHENTICATION_COMPLETE_EVENT, hci.HCI_ENCRYPTION_CHANGE_EVENT,
    hci.HCI_ENCRYPTION_CHANGE_V2_EVENT, hci.HCI_CHANGE_CONNECTION_LINK_KEY_COMPLETE_EVENT,
    hci.HCI_LINK_KEY_TYPE_CHANGED_EVENT, hci.HCI_RETURN_LINK_KEYS_EVENT,
    hci.HCI_PIN_CODE_REQUEST_EVENT, hci.HCI_LINK_KEY_REQUEST_EVENT,
    hci.HCI_LINK_KEY_NOTIFICATION_EVENT, hci.HCI_ENCRYPTION_KEY_REFRESH_COMPLETE_EVENT,
    hci.HCI_IO_CAPABILITY_REQUEST_EVENT, hci.HCI_IO_CAPABILITY_RESPONSE_EVENT,
    hci.HCI_USER_CONFIRMATION_REQUEST_EVENT, hci.HCI_USER_PASSKEY_REQUEST_EVENT,
    hci.HCI_REMOTE_OOB_DATA_REQUEST_EVENT, hci.HCI_SIMPLE_PAIRING_COMPLETE_EVENT,
    hci.HCI_USER_PASSKEY_NOTIFICATION_EVENT, hci.HCI_KEYPRESS_NOTIFICATION_EVENT,
}


def emit(stage, **values):
    print(json.dumps({"stage": stage, **values}), flush=True)


class NativeTransport:
    def __init__(self):
        self.lib = ctypes.CDLL(str(ROOT / "libacipc_bridge.dylib"))
        self.lib.acipc_open.argtypes = []
        self.lib.acipc_open.restype = ctypes.c_int
        self.lib.acipc_close.argtypes = []
        self.lib.acipc_close.restype = None
        self.lib.acipc_send.argtypes = [ctypes.c_uint8, ctypes.c_void_p, ctypes.c_size_t]
        self.lib.acipc_send.restype = ctypes.c_int
        self.lib.acipc_recv.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_void_p,
                                       ctypes.c_size_t, ctypes.c_int]
        self.lib.acipc_recv.restype = ctypes.c_int
        self.sink = None
        self.running = False
        self.failure = None
        self.pump_task = None
        self.sent = 0
        self.received = 0

    def set_packet_sink(self, sink):
        self.sink = sink

    def on_packet(self, packet):
        raw = bytes(packet)
        if not raw or raw[0] not in (1, 2, 3):
            raise RuntimeError("unsupported outbound packet type")
        if raw[0] == 1:
            opcode = int.from_bytes(raw[1:3], "little")
            # Never load firmware, vendor commands, stored keys, authentication or pairing.
            if (opcode >> 10) == 0x3F or opcode in NO_PAIR_COMMANDS:
                self.failure = RuntimeError("command excluded from no-pair probe")
                raise self.failure
        payload = ctypes.create_string_buffer(raw[1:])
        status = self.lib.acipc_send(raw[0], payload, len(raw) - 1)
        if status != 0:
            self.failure = RuntimeError(f"native send failed: {status}")
            raise self.failure
        self.sent += 1

    def read_one(self):
        kind = ctypes.c_uint8()
        buf = ctypes.create_string_buffer(4096)
        count = self.lib.acipc_recv(ctypes.byref(kind), buf, len(buf), 10)
        if count < 0:
            raise RuntimeError(f"native receive failed: {count}")
        return bytes([kind.value]) + buf.raw[:count] if count else None

    async def pump(self):
        try:
            while self.running:
                packet = await asyncio.to_thread(self.read_one)
                if packet is None:
                    continue
                # Stop before the stack sees any request that could cause pairing/key changes.
                if packet[0] == 4 and packet[1] in NO_PAIR_EVENTS:
                    raise RuntimeError("phone authentication or pairing required; no keys changed")
                if packet[0] == 4 and packet[1] == hci.HCI_DISCONNECTION_COMPLETE_EVENT and len(packet) >= 7:
                    emit("controller-link-disconnected", status=packet[3], hci_reason=packet[6])
                self.received += 1
                if self.sink:
                    self.sink.on_packet(packet)
        except Exception as error:
            self.failure = error
            self.running = False

    async def start(self):
        status = self.lib.acipc_open()
        if status != 0:
            raise RuntimeError(f"native open failed: {status}")
        self.running = True
        self.pump_task = asyncio.create_task(self.pump())

    async def stop(self):
        self.running = False
        cancelled = False
        if self.pump_task:
            # Task cancellation must not close a channel while its native read still runs.
            # The parent process timeout remains the hard limit for a blocked native call.
            while not self.pump_task.done():
                try:
                    await asyncio.shield(self.pump_task)
                except asyncio.CancelledError:
                    cancelled = True
            self.pump_task.result()
        self.lib.acipc_close()
        emit("transport-closed", sent=self.sent, received=self.received)
        if cancelled:
            raise asyncio.CancelledError
        if self.failure:
            raise self.failure

    async def guarded(self, awaitable, timeout):
        task = asyncio.create_task(awaitable)
        try:
            end = asyncio.get_running_loop().time() + timeout
            while not task.done():
                if self.failure:
                    raise self.failure
                if asyncio.get_running_loop().time() >= end:
                    raise TimeoutError("operation exceeded bound")
                await asyncio.sleep(0.01)
            if self.failure:
                raise self.failure
            return await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


class NoFirmwareHost(Host):
    async def reset(self, driver_factory=None):
        # Use standard initialization only; prohibit automatic vendor driver/firmware loading.
        await super().reset(driver_factory=None)


async def run_probe(target, hfp_control=False):
    transport = NativeTransport()
    host = NoFirmwareHost(controller_source=transport, controller_sink=transport)
    config = DeviceConfiguration(name="AI-Phone", le_enabled=False,
                                 classic_enabled=True, classic_smp_enabled=False,
                                 classic_accept_any=False, connectable=False,
                                 discoverable=False, keystore=None)
    device = Device(config=config, host=host)
    device.keystore = MemoryKeyStore()
    device.command_timeout = 3
    target_address = hci.Address(target, hci.Address.PUBLIC_DEVICE_ADDRESS)
    discovered = set()
    device.on(device.EVENT_INQUIRY_RESULT,
              lambda address, *_: discovered.add(str(address)))
    connection = None
    client = None
    rfcomm_client = None
    primary_error = None
    inquiry_pending = False
    await transport.start()
    try:
        await transport.guarded(device.power_on(), 15)
        if host.local_version is None or host.local_version.status:
            raise RuntimeError("initialized host has no successful version response")
        emit("native-version", hci_version=int(host.local_version.hci_version))
        emit("independent-stack-ready", classic=True, firmware_driver=False)
        await transport.guarded(device.send_sync_command(
            hci.HCI_Write_Inquiry_Mode_Command(inquiry_mode=hci.HCI_EXTENDED_INQUIRY_MODE)), 3)
        inquiry_complete = asyncio.Event()
        device.once(device.EVENT_INQUIRY_COMPLETE, inquiry_complete.set)
        device.auto_restart_inquiry = False
        inquiry_pending = True
        await transport.guarded(device.send_async_command(hci.HCI_Inquiry_Command(
            lap=hci.HCI_GENERAL_INQUIRY_LAP, inquiry_length=4, num_responses=0)), 3)
        await transport.guarded(inquiry_complete.wait(), 7)
        inquiry_pending = False
        if transport.failure:
            raise transport.failure
        emit("inquiry-result", total=len(discovered), target_seen=str(target_address) in discovered)
        connection = await transport.guarded(device.connect_classic(target_address, timeout=8), 10)
        emit("phone-acl-connected", target="selected-phone")
        client = sdp.Client(connection)
        await transport.guarded(client.connect(), 5)
        handles = await transport.guarded(client.search_services([core.UUID.from_16_bits(0x111F)]), 5)
        emit("phone-sdp", handsfree_audio_gateway=bool(handles), records=len(handles))
        if not handles:
            raise RuntimeError("selected phone has no HFP audio-gateway service")
        if hfp_control:
            records = await transport.guarded(client.search_attributes(
                [core.UUID.from_16_bits(0x111F)],
                [sdp.SDP_PROTOCOL_DESCRIPTOR_LIST_ATTRIBUTE_ID]), 5)
            channels = set()
            for record in records:
                for attribute in record:
                    for descriptor in attribute.value.value:
                        elements = descriptor.value
                        if len(elements) >= 2 and elements[0].value == core.BT_RFCOMM_PROTOCOL_ID:
                            channels.add(elements[1].value)
            if len(channels) != 1:
                raise RuntimeError("phone HFP RFCOMM channel is missing or ambiguous")
            server_channel = channels.pop()
            if not isinstance(server_channel, int) or not 1 <= server_channel <= 30:
                raise RuntimeError("invalid HFP RFCOMM server channel")
            await transport.guarded(client.disconnect(), 2)
            rfcomm_client = rfcomm.Client(connection)
            emit("rfcomm-starting")
            multiplexer = await transport.guarded(rfcomm_client.start(), 5)
            emit("rfcomm-ready")
            dlc = await transport.guarded(multiplexer.open_dlc(server_channel), 5)
            emit("hfp-dlc-ready")
            protocol = hfp.HfProtocol(dlc, hfp.HfConfiguration(
                supported_hf_features=[], supported_hf_indicators=[],
                supported_audio_codecs=[hfp.AudioCodec.CVSD]))
            # initiate_slc only exchanges features/indicators and enables notifications.
            # Do not call run(), audio setup, dial, answer, hangup or voice recognition.
            await transport.guarded(protocol.initiate_slc(), 8)
            emit("hfp-control-ready", indicators=len(protocol.ag_indicators), audio_started=False)
    except asyncio.CancelledError as error:
        primary_error = error
        emit("operation-cancelled")
        raise
    except Exception as error:
        primary_error = error
        emit("operation-failed", error_type=type(error).__name__, reason=str(error))
        raise
    finally:
        cleanup_error = None
        if inquiry_pending and not transport.failure:
            try:
                await transport.guarded(device.send_sync_command(hci.HCI_Inquiry_Cancel_Command()), 2)
            except Exception as error:
                cleanup_error = error
                emit("inquiry-cleanup-failed", error_type=type(error).__name__)
        if rfcomm_client and rfcomm_client.multiplexer and not transport.failure:
            try:
                await transport.guarded(rfcomm_client.shutdown(), 3)
                emit("hfp-control-disconnected")
            except Exception as error:
                cleanup_error = error
                emit("rfcomm-cleanup-failed", error_type=type(error).__name__)
        if client and client.channel and not transport.failure:
            try:
                await transport.guarded(client.disconnect(), 2)
            except Exception as error:
                cleanup_error = error
                emit("sdp-cleanup-failed", error_type=type(error).__name__)
        if connection and not transport.failure:
            try:
                await transport.guarded(connection.disconnect(), 2)
                emit("phone-acl-disconnected")
            except Exception as error:
                cleanup_error = error
                emit("acl-cleanup-failed", error_type=type(error).__name__)
        try:
            await transport.stop()
        except Exception as error:
            if primary_error is None:
                raise
            emit("transport-cleanup-failed", error_type=type(error).__name__)
        if cleanup_error and primary_error is None:
            raise RuntimeError("phone cleanup did not complete") from cleanup_error


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--hfp", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(asyncio.wait_for(run_probe(args.target, args.hfp), 45))
    except asyncio.CancelledError as error:
        emit("probe-failed", error_type=type(error).__name__, reason=str(error))
        raise SystemExit(1)
    except Exception as error:
        emit("probe-failed", error_type=type(error).__name__, reason=str(error))
        raise SystemExit(1)
