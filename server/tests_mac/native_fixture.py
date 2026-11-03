"""Replace hardware boundaries, not native.run, SLC, HFP parsing or adapters."""
import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch
from pyee import EventEmitter
from bumble import core, hci
from server.mac.native import acipc_pair as native


class NativeFixture:
    PHONE = 'AA:BB:CC:DD:EE:FF'

    def __init__(self):
        self.started = self.stopped = False
        self.rfcomm_shutdown = self.acl_disconnect = 0
        self.commands = []
        self.call_state = 3
        self.fail_command = None
        self.cleanup_error = None
        self.release_gate = None
        # 手机报告的蓝牙名称；设为异常则名称请求失败，设置 name_gate 则名称请求挂起。
        self.remote_name = 'Fixture Phone'
        self.name_gate = None
        self.name_requests = 0
        channel = EventEmitter()
        channel.EVENT_CLOSE = 'close'
        self.channel = channel
        self.dlc = SimpleNamespace(multiplexer=SimpleNamespace(l2cap_channel=channel), write=self.write)
        owner = self

        class Connection(EventEmitter):
            EVENT_DISCONNECTION = 'disconnection'
            handle = 1
            is_encrypted = True
            async def authenticate(self): pass
            async def encrypt(self): pass
            async def disconnect(self):
                if owner.cleanup_error:
                    raise owner.cleanup_error
                owner.acl_disconnect += 1
                self.emit(self.EVENT_DISCONNECTION, 0)
                channel.emit('close')
        self.connection = Connection()

        class Transport:
            failure = None
            def __init__(self, *_): pass
            async def start(self): owner.started = True
            async def stop(self):
                if owner.release_gate is not None:
                    # Native read still running: the release cannot complete yet.
                    await owner.release_gate.wait()
                owner.stopped = True
            async def guarded(self, operation, timeout):
                return await asyncio.wait_for(operation, timeout)
        self.Transport = Transport

        class Device:
            EVENT_SCO_REQUEST = 'sco-request'
            EVENT_SCO_CONNECTION = 'sco-connected'
            EVENT_SCO_CONNECTION_FAILURE = 'sco-failure'
            public_address = hci.Address('11:22:33:44:55:66')
            def __init__(self, **_): pass
            async def power_on(self): pass
            async def connect_classic(self, *_, **__): return owner.connection
            async def request_remote_name(self, remote):
                assert remote is owner.connection
                owner.name_requests += 1
                if owner.name_gate is not None:
                    await owner.name_gate.wait()
                if isinstance(owner.remote_name, BaseException):
                    raise owner.remote_name
                return owner.remote_name
        self.Device = Device

        class PrivateStore:
            failure = None
            def __init__(self, *_): pass
            async def get(self, *_):
                return SimpleNamespace(link_key=SimpleNamespace(authenticated=True))
        self.PrivateStore = PrivateStore

        class SDP:
            def __init__(self, *_): self.channel = None
            async def connect(self): self.channel = object()
            async def disconnect(self): self.channel = None
            async def search_attributes(self, *_):
                descriptor = SimpleNamespace(value=[SimpleNamespace(value=core.BT_RFCOMM_PROTOCOL_ID), SimpleNamespace(value=1)])
                return [[SimpleNamespace(value=SimpleNamespace(value=[descriptor]))]]
        self.SDP = SDP

        class RFCOMM:
            def __init__(self, *_): self.multiplexer = None
            async def start(self): self.multiplexer = self; return self
            async def open_dlc(self, *_): return owner.dlc
            async def shutdown(self):
                if owner.cleanup_error:
                    raise owner.cleanup_error
                owner.rfcomm_shutdown += 1
        self.RFCOMM = RFCOMM

    def write(self, command):
        command = command.strip()
        self.commands.append(command)
        if command == self.fail_command:
            self.dlc.sink(b'\r\nERROR\r\n')
            return
        if command == 'AT+CHUP':
            self.call_state = None
            response = ''
        elif command.startswith('ATD'):
            self.call_state = 2
            response = ''
        elif command == 'AT+CLCC':
            response = '' if self.call_state is None else f'+CLCC: 1,0,{self.call_state},0,0,"fixture",129'
        else:
            response = {'AT+BRSF=0': '+BRSF: 0',
                        'AT+CIND=?': '+CIND: ("call",(0,1)),("callsetup",(0-3)),("callheld",(0-2))',
                        'AT+CIND?': '+CIND: 0,3,0', 'AT+CMER=3,,,1': ''}[command]
        self.dlc.sink(((f'\r\n{response}\r\n' if response else '') + '\r\nOK\r\n').encode())

    def notify(self, data):
        self.dlc.sink(data)

    def lose_acl(self):
        self.connection.emit('disconnection', 8)
        self.channel.emit('close')

    def lose_control_channel(self):
        # The phone link is gone but the controller reported no disconnection event.
        self.channel.emit('close')

    @contextmanager
    def patched(self):
        with (patch.multiple(native, SelectedTransport=self.Transport, NoFirmwareHost=lambda **_: None,
                             Device=self.Device, SelectedKeyStore=self.PrivateStore, emit=lambda *_, **__: None),
              patch.object(native.sdp, 'Client', self.SDP), patch.object(native.rfcomm, 'Client', self.RFCOMM)):
            yield self
