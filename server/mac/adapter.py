"""Mac HFP/SCO adapters for the existing gateway API and PCM contract."""
import asyncio
import audioop
import contextlib
import time
from collections import deque

from bumble import hci, hfp

from server.common.contracts import GatewayError, address, phone_number
from .hfp_control import HFPControl, StaleIdentity

STATES = {0: 'active', 1: 'held', 2: 'dialing', 3: 'alerting', 4: 'incoming', 5: 'waiting'}
OUTGOING, INCOMING = 0, 1


class MacPhoneAdapter:
    # 接听结果以手机 CLCC 状态为准：ATA 的回复不可信（真机回错误状态码但已接通）。
    ANSWER_SECONDS = 5

    def __init__(self, selected, device, connection, protocol, transport, connection_epoch=0):
        self.address = address(selected)
        self.device, self.connection = device, connection
        self.protocol, self.transport = protocol, transport
        self.control = HFPControl(protocol)
        protocol.execute_command = self.control.execute
        self.link = None
        self.audio = None
        # SCO 代次：每条建立的链路一个代次；单次链路错误与连接级错误分开保存。
        self.audio_epoch = 0
        self.audio_failure = None
        self.sco_ready = asyncio.Event()
        self.accept_tasks = set()
        self.failure = None
        self.acl_closed = False
        self.call_attempted = False
        self.owned_call_id = None
        self.owned_signature = None
        # 身份 = CLCC 行 + 本连接内的通话代次 + 连接代次；重连后旧身份不再匹配任何电话。
        self.call_epoch = 0
        self.connection_epoch = connection_epoch
        protocol.on(hfp.HfProtocol.EVENT_AG_INDICATOR, self.call_indicator)
        self.rx_packets = self.tx_packets = self.bad_packets = 0
        connection.on(device.EVENT_SCO_REQUEST, self.requested)
        connection.on(device.EVENT_SCO_CONNECTION, self.connected)
        connection.on(device.EVENT_SCO_CONNECTION_FAILURE, self.sco_failed)
        connection.on('disconnection', self.acl_disconnected)

    def acl_disconnected(self, *_):
        self.acl_closed = True
        self.failure = GatewayError('phone_disconnected', 503)

    async def start(self):
        pass

    async def device_path(self, selected):
        if address(selected) != self.address:
            raise GatewayError('device_not_found', 404)
        if self.acl_closed:
            raise self.failure
        return self.address

    async def devices(self):
        return [{'address': self.address, 'paired': True,
                 'connected': not self.transport.failure}]

    async def capabilities(self):
        return {'platform': 'macOS', 'bluez': False, 'nativeTelephony': True,
                'phones': [{'address': self.address, 'methods': ['Dial', 'Hangup', 'Answer', 'SendTones']}],
                'audio': {'rate': 24000, 'channels': 1, 'format': 'pcm16le',
                          'maxFrameBytes': 960, 'singleOwner': True,
                          'maxQueuedMilliseconds': 200, 'readyTimeoutSeconds': 45},
                'control': {'leaseSeconds': 30}}

    async def connect(self, selected, connected):
        await self.device_path(selected)
        # Radio lifecycle belongs to the Mac service wrapper, not the UI lease.
        if not connected:
            raise GatewayError('unsupported_capability', 503)

    async def call_info(self):
        try:
            await self.device_path(self.address)
            calls = await self.transport.guarded(self.protocol.query_current_calls(), 5)
            if self.acl_closed:
                raise GatewayError('phone_disconnected', 503)
            if self.transport.failure:
                raise GatewayError('native_transport_failed', 503)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if self.acl_closed:
                raise GatewayError('phone_disconnected', 503) from error
            if self.transport.failure:
                raise GatewayError('native_transport_failed', 503) from error
            if isinstance(error, TimeoutError):
                raise GatewayError('hfp_query_timeout', 503) from error
            if isinstance(error, ConnectionError):
                raise GatewayError('hfp_query_failed', 503) from error
            raise
        if self.owned_signature and not any(self.signature(c) == self.owned_signature for c in calls):
            self.call_attempted = False
            self.owned_call_id = self.owned_signature = None
        return calls

    def signature(self, call):
        return (call.index, int(call.direction), call.number, call.type, self.call_epoch, self.connection_epoch)

    def current(self, identity):
        return isinstance(identity, tuple) and len(identity) == 6 and identity[4:] == (self.call_epoch, self.connection_epoch)

    def owns(self, call_id, identity):
        return bool(identity) and self.owned_signature == identity and self.owned_call_id == call_id

    def call_indicator(self, value):
        if value.indicator not in (hfp.AgIndicator.CALL, hfp.AgIndicator.CALL_SETUP, hfp.AgIndicator.CALL_HELD):
            return
        indicators = {i.indicator: i.current_status for i in self.protocol.ag_indicators}
        if hfp.AgIndicator.CALL in indicators and hfp.AgIndicator.CALL_SETUP in indicators and all(
            indicators.get(key, 0) == 0 for key in (hfp.AgIndicator.CALL, hfp.AgIndicator.CALL_SETUP, hfp.AgIndicator.CALL_HELD)
        ):
            # A confirmed no-calls notification invalidates even a reused identical CLCC row,
            # owned or not: an unanswered incoming call that ends and rings again is a new call.
            self.call_epoch += 1
            self.call_attempted = False
            self.owned_call_id = self.owned_signature = None

    def own(self, call, direction=OUTGOING):
        if int(call.direction) != direction:
            raise GatewayError('dial_call_not_identified', 409)
        self.owned_call_id = f'call{call.index}'
        self.owned_signature = self.signature(call)

    async def calls(self, selected):
        await self.device_path(selected)
        calls = await self.call_info()
        return [{'id': f'call{call.index}', 'state': STATES.get(int(call.status), 'unknown'),
                 'multiparty': bool(call.multi_party)} for call in calls]

    async def peer_number(self, selected, call_id):
        """Numeric caller ID for one call. The call list still omits numbers and names."""
        await self.device_path(selected)
        call = next((item for item in await self.call_info() if f'call{item.index}' == call_id), None)
        return phone_number(getattr(call, 'number', None)) if call else None

    async def call_identity(self, selected, call_id):
        """本连接拥有的通话返回其身份；尚未接听的来电也返回身份（不代表拥有，不能被挂断）。"""
        await self.device_path(selected)
        calls = await self.call_info()
        if self.owned_call_id == call_id:
            return self.owned_signature
        call = next((c for c in calls if f'call{c.index}' == call_id), None)
        if call is not None and int(call.direction) == INCOMING and STATES.get(int(call.status)) in ('incoming', 'waiting'):
            return self.signature(call)
        return None

    def subscribe_calls(self, selected, callback):
        if address(selected) != self.address:
            raise GatewayError('device_not_found', 404)
        def indicator(value):
            if value.indicator in (hfp.AgIndicator.CALL, hfp.AgIndicator.CALL_SETUP, hfp.AgIndicator.CALL_HELD):
                callback()
        def disconnected(*_):
            callback()
        self.protocol.on(hfp.HfProtocol.EVENT_AG_INDICATOR, indicator)
        self.connection.on('disconnection', disconnected)
        def unsubscribe():
            self.protocol.remove_listener(hfp.HfProtocol.EVENT_AG_INDICATOR, indicator)
            self.connection.remove_listener('disconnection', disconnected)
        return unsubscribe

    async def call_snapshot(self, selected, call_id, identity):
        await self.device_path(selected)
        if not self.current(identity):
            return 'ended'
        calls = await self.call_info()
        call = next((c for c in calls if f'call{c.index}' == call_id and self.signature(c) == identity), None)
        return STATES.get(int(call.status), 'unknown') if call else 'ended'

    async def hangup_owned(self, selected, call_id, identity):
        """True: 已对仍是本次身份的电话发出挂断。False: 本次身份已不在。异常: 结果未知。"""
        await self.device_path(selected)
        if not self.current(identity):
            return False
        calls = await self.call_info()
        if not self.owns(call_id, identity):
            return False
        if len(calls) != 1:
            raise GatewayError('ambiguous_call', 409)

        def still_owned():
            if self.acl_closed:
                raise self.failure
            return self.owns(call_id, identity)
        try:
            await self.transport.guarded(self.control.execute('AT+CHUP', guard=still_owned), 5)
        except StaleIdentity:
            return False
        self.call_attempted = False
        self.owned_call_id = self.owned_signature = None
        return True

    async def operate(self, selected, operation, value=None, call_id=None):
        await self.device_path(selected)
        current = await self.calls(selected)
        if operation == 'dial':
            if current:
                raise GatewayError('phone_busy', 409)
            self.call_attempted = True
            try:
                await self.transport.guarded(self.protocol.execute_command(f'ATD{value};', timeout=3), 5)
            except Exception:
                # A lost AT response does not prove the phone failed to dial.
                observed = await self.call_info()
                if len(observed) == 1 and int(observed[0].direction) == 0:
                    self.own(observed[0])
                else:
                    self.call_attempted = False
                raise
            for _ in range(20):
                observed = await self.call_info()
                if len(observed) == 1:
                    self.own(observed[0])
                    current = await self.calls(selected)
                    if not current or not self.owned_signature:
                        raise GatewayError('call_not_found', 409)
                    return {'call': current[0]}
                if len(observed) > 1:
                    raise GatewayError('ambiguous_call', 409)
                await asyncio.sleep(0.1)
            raise GatewayError('call_not_found', 409)
        if call_id is not None and not any(c['id'] == call_id for c in current):
            raise GatewayError('call_not_found', 404)
        if operation == 'answer':
            return await self.answer(call_id)
        if operation == 'hangup':
            if len(current) != 1:
                raise GatewayError('ambiguous_call', 409)
            await self.transport.guarded(self.protocol.terminate_call(), 5)
            self.call_attempted = False
            self.owned_call_id = None
            self.owned_signature = None
            return {'ok': True}
        if operation == 'tones':
            for tone in value:
                await self.transport.guarded(self.protocol.execute_command(f'AT+VTS={tone}'), 3)
            return {'ok': True}
        raise GatewayError('unsupported_capability', 503)

    async def answer(self, call_id):
        """接听唯一一通响铃中的来电。以 CLCC 确认：有界时间内变为 active 才算成功。

        发送 ATA 前把这通来电记为本连接拥有，使最后一刻核对生效。只有确认 active 才保留
        拥有关系；未确认（超时、取消、查询失败）时一律释放，关闭连接时不会拒接仍在响铃的来电。
        """
        calls = await self.call_info()
        call = next((c for c in calls if f'call{c.index}' == call_id), None)
        if call is None:
            raise GatewayError('call_not_found', 404)
        if len(calls) != 1:
            raise GatewayError('ambiguous_call', 409)
        if int(call.direction) != INCOMING or STATES.get(int(call.status)) != 'incoming':
            raise GatewayError('call_not_incoming', 409)
        identity = self.signature(call)
        deadline = asyncio.get_running_loop().time() + self.ANSWER_SECONDS
        self.call_attempted = True
        self.own(call, INCOMING)

        def still_owned():
            if self.acl_closed:
                raise self.failure
            return self.owns(call_id, identity)
        try:
            return await self.confirm_answer(call_id, identity, deadline, still_owned)
        except BaseException:
            if self.owns(call_id, identity):
                self.call_attempted = False
                self.owned_call_id = self.owned_signature = None
            raise

    async def confirm_answer(self, call_id, identity, deadline, still_owned):
        try:
            await self.transport.guarded(self.control.execute('ATA', timeout=3, guard=still_owned), self.ANSWER_SECONDS)
        except StaleIdentity:
            raise GatewayError('call_not_found', 409) from None
        except asyncio.CancelledError:
            raise
        except Exception:
            # 错误状态码或丢失的回复都不能证明手机没有接听；以下以 CLCC 为准。
            pass
        while True:
            calls = await self.call_info()
            if not self.owns(call_id, identity):
                raise GatewayError('call_not_found', 409)
            call = next(c for c in calls if self.signature(c) == identity)
            if STATES.get(int(call.status)) == 'active':
                return {'ok': True}
            if asyncio.get_running_loop().time() >= deadline:
                raise GatewayError('answer_not_confirmed', 503)
            await asyncio.sleep(0.1)

    def requested(self, link_type):
        # 新的 SCO 生命周期开始：只清除上一条链路的音频错误，不清除 ACL/native 故障。
        epoch = self.audio_epoch
        self.audio_failure = None

        async def accept():
            try:
                kind = hfp.DefaultCodecParameters.SCO_CVSD_D1 if link_type == 0 else hfp.DefaultCodecParameters.ESCO_CVSD_S1
                await self.transport.guarded(self.device.send_async_command(
                    hci.HCI_Enhanced_Accept_Synchronous_Connection_Request_Command(
                        bd_addr=self.connection.peer_address,
                        **hfp.ESCO_PARAMETERS[kind].asdict())), 5)
            except Exception:
                if epoch == self.audio_epoch and self.link is None:
                    self.audio_failure = GatewayError('sco_accept_failed', 503)
                    self.sco_ready.set()
        task = asyncio.create_task(accept())
        self.accept_tasks.add(task)
        task.add_done_callback(self.accept_tasks.discard)

    def connected(self, link):
        previous = self.link
        if previous is not None and previous is not link:
            previous.sink = None
        self.audio_epoch += 1
        self.audio_failure = None
        self.link = link
        link.sink = lambda packet: self.receive(link, packet)
        link.on(link.EVENT_DISCONNECTION, lambda *_: self.disconnected(link))
        self.sco_ready.set()

    def disconnected(self, link):
        # 只有当前链路的断开才清空 link；迟到的旧链路断开不影响新 link。
        link.sink = None
        if self.link is link:
            self.link = None
            self.sco_ready.clear()

    def sco_failed(self, _status):
        # 失败事件只属于尚未建立的请求；已建立的链路以断开事件报告故障。
        if self.link is None:
            self.audio_failure = GatewayError('sco_failed', 503)
            self.sco_ready.set()

    def receive(self, link, packet):
        if self.link is not link:
            return
        data = packet.data
        if not data or len(data) % 2:
            self.audio_failure = GatewayError('invalid_sco_pcm', 503)
            return
        self.rx_packets += 1
        if packet.packet_status != hci.HCI_SynchronousDataPacket.Status.CORRECTLY_RECEIVED_DATA:
            self.bad_packets += 1
            data = bytes(len(data))
        start = time.monotonic()
        outgoing = self.audio.receive(data) if self.audio else bytes(len(data))
        self.device.host.send_sco_sdu(self.link.handle, outgoing)
        self.tx_packets += 1
        if self.audio and self.audio.metrics:
            self.audio.metrics.observe("hfp_host_send", time.monotonic()-start, len(outgoing))

    async def close(self):
        self.protocol.remove_listener(hfp.HfProtocol.EVENT_AG_INDICATOR, self.call_indicator)
        self.connection.remove_listener(self.device.EVENT_SCO_REQUEST, self.requested)
        for task in tuple(self.accept_tasks):
            task.cancel()
        await asyncio.gather(*tuple(self.accept_tasks), return_exceptions=True)
        if self.audio:
            await self.audio.close()
        cleanup_error = None
        if self.call_attempted and not self.transport.failure and not self.acl_closed:
            try:
                current = await self.call_info()
                if current:
                    if not self.call_attempted:
                        current = []
                    elif len(current) != 1 or not self.owned_signature or self.signature(current[0]) != self.owned_signature:
                        raise GatewayError('call_cleanup_ambiguous', 503)
                # 绝不对响铃中的电话发挂断：那等于拒接。
                if current and STATES.get(int(current[0].status)) not in ('incoming', 'waiting'):
                    await self.transport.guarded(self.protocol.terminate_call(), 5)
            except Exception as error:
                cleanup_error = error
        if self.link and not self.transport.failure and not self.acl_closed:
            self.link.sink = None
            await self.transport.guarded(self.link.disconnect(), 3)
        self.connection.remove_listener(self.device.EVENT_SCO_CONNECTION, self.connected)
        self.connection.remove_listener(self.device.EVENT_SCO_CONNECTION_FAILURE, self.sco_failed)
        self.connection.remove_listener('disconnection', self.acl_disconnected)
        if cleanup_error:
            raise GatewayError('call_cleanup_failed', 503) from None


class MacSCOAudio:
    MAX_PLAYBACK_BYTES = 3200  # 200 ms at 8 kHz PCM16, not a pacing delay.

    def __init__(self, selected, adapter):
        self.address = address(selected)
        self.adapter = adapter
        self.capture = asyncio.Queue(maxsize=10)
        self.input = bytearray()
        self.output = bytearray()
        self.up_state = self.down_state = None
        self.drained = asyncio.Event()
        self.failure = None
        self.closed = False
        self.input_bytes = self.output_bytes = 0
        self.metrics = None
        self.capture_times = deque()
        self.playback_times = deque()

    async def start(self):
        await self.adapter.device_path(self.address)
        await self.adapter.sco_ready.wait()
        self.check()
        if self.adapter.audio:
            raise GatewayError('audio_busy', 409)
        self.adapter.audio = self

    def check(self):
        if self.failure:
            raise self.failure
        if self.adapter.failure:
            raise self.adapter.failure
        if self.adapter.transport.failure:
            raise GatewayError('native_transport_failed', 503)
        if self.adapter.audio_failure:
            raise self.adapter.audio_failure
        if self.closed or not self.adapter.link:
            raise GatewayError('sco_closed', 409)

    def receive(self, pcm):
        if self.failure or self.closed:
            return bytes(len(pcm))
        start = time.monotonic()
        self.input.extend(pcm)
        while len(self.input) >= 320:
            frame = bytes(self.input[:320])
            del self.input[:320]
            converted, self.up_state = audioop.ratecv(frame, 2, 1, 8000, 24000, self.up_state)
            try:
                self.capture.put_nowait(converted)
                self.capture_times.append(time.monotonic())
                self.input_bytes += len(converted)
            except asyncio.QueueFull:
                self.failure = GatewayError('audio_backpressure', 409)
                return bytes(len(pcm))
        count = min(len(pcm), len(self.output))
        outgoing = bytes(self.output[:count]) + bytes(len(pcm) - count)
        del self.output[:count]
        remaining = count
        while remaining and self.playback_times:
            size, arrived = self.playback_times.popleft()
            used = min(size, remaining)
            if self.metrics:
                self.metrics.observe('hfp_playback_queue', time.monotonic()-arrived, used, len(self.output))
            remaining -= used
            if used < size:
                self.playback_times.appendleft((size-used, arrived))
        if self.metrics:
            self.metrics.observe('hfp_capture_process', time.monotonic()-start, len(pcm), self.capture.qsize())
            if count < len(pcm):
                self.metrics.observe('hfp_underrun', size=len(pcm)-count)
        self.drained.set()
        return outgoing

    async def read(self):
        self.check()
        data = await self.capture.get()
        arrived = self.capture_times.popleft() if self.capture_times else time.monotonic()
        if self.metrics:
            self.metrics.observe('hfp_capture_queue', time.monotonic()-arrived, len(data), self.capture.qsize())
        return data

    async def write(self, pcm):
        self.check()
        if not pcm or len(pcm) % 2 or len(pcm) > 960:
            raise GatewayError('invalid_pcm_frame', 400)
        converted, next_state = audioop.ratecv(pcm, 2, 1, 24000, 8000, self.down_state)
        while len(self.output) + len(converted) > self.MAX_PLAYBACK_BYTES:
            self.drained.clear()
            self.check()
            try:
                await asyncio.wait_for(self.drained.wait(), 0.2)
            except asyncio.TimeoutError:
                raise GatewayError('audio_backpressure', 409) from None
        self.check()
        self.down_state = next_state
        self.output.extend(converted)
        if converted:
            self.playback_times.append((len(converted), time.monotonic()))
        self.output_bytes += len(pcm)

    async def monitor(self):
        while True:
            self.check()
            await asyncio.sleep(0.05)

    async def close(self):
        self.closed = True
        if self.adapter.audio is self:
            self.adapter.audio = None
        if self.metrics:
            self.metrics.observe("playback_queue_clear", size=len(self.output))
        self.input.clear()
        self.output.clear()
        self.capture_times.clear()
        self.playback_times.clear()
        self.up_state = self.down_state = None
        while not self.capture.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self.capture.get_nowait()
        self.drained.set()
