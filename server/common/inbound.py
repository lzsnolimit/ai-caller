"""来电监听与自动接听：对每个已连接设备订阅通话变化，线路空闲时约 3 s 后接听。"""
import asyncio
import contextlib
import functools
from pathlib import Path

from .contracts import GatewayError

INBOUND_ANSWER_DELAY = 3
QUERY_SECONDS = 6
GUARD_PROMPT_PATH = Path(__file__).parent / 'inbound-guard-prompt.md'
INBOUND_PURPOSE = '来电：礼貌询问并记录对方的身份和来意'
INBOUND_OUTCOME = '弄清对方是谁、有什么事、是否需要回电；不回答问题，不透露任何我方信息'
DEFAULT_INBOUND_PROMPT = ('机主现在不方便接电话。请用对方的语言礼貌地问清楚对方是谁、有什么事、是否需要回电，'
                          '复述确认后结束通话。')


def load_guard_prompt():
    """固定文件，服务启动时读取一次。缺失是打包错误，直接失败。"""
    content = GUARD_PROMPT_PATH.read_text(encoding='utf-8').strip()
    if not content:
        raise RuntimeError('inbound guard prompt is empty')
    return content


class InboundWatcher:
    """只在线路空闲、设备未隔离、无外部租约时接听；其他情况不拒接、不挂断。"""
    def __init__(self, runtime):
        self.runtime = runtime
        self.subscriptions = {}   # address -> unsubscribe
        self.candidates = {}      # address -> answer task for one ringing call
        self.pending = set()      # addresses whose call list must be inspected
        self.refresh = False
        self.wake = asyncio.Event()
        self.task = None
        self.closed = False

    def start(self):
        if self.task is None and not self.closed:
            self.devices_changed()
            self.task = asyncio.create_task(self.run())

    def devices_changed(self):
        self.refresh = True
        self.wake.set()

    def changed(self, selected):
        self.pending.add(selected)
        self.wake.set()

    def eligible(self, selected):
        runtime = self.runtime
        return (not self.closed and not runtime.closing and selected in self.subscriptions
                and runtime.line.idle() and selected not in runtime.recovering and not runtime.hub.lease)

    async def run(self):
        while not self.closed:
            await self.wake.wait()
            self.wake.clear()
            if self.refresh:
                self.refresh = False
                await self.sync()
            while self.pending:
                await self.inspect(self.pending.pop())
            await asyncio.sleep(0.05)

    async def sync(self):
        """设备连接/断开时增删订阅。adapter 缺失或订阅失败不报错，下次通知再试。"""
        adapter = self.runtime.adapter
        try:
            devices = await asyncio.wait_for(adapter.devices(), QUERY_SECONDS)
        except Exception:
            return
        live = {device['address'] for device in devices if device.get('connected')}
        for selected in tuple(self.subscriptions):
            if selected not in live:
                with contextlib.suppress(Exception):
                    self.subscriptions.pop(selected)()
                self.drop_candidate(selected)
        subscribe = getattr(adapter, 'subscribe_calls', None)
        if not subscribe:
            return
        for selected in live - self.subscriptions.keys():
            try:
                self.subscriptions[selected] = subscribe(selected, functools.partial(self.changed, selected))
            except Exception:
                continue
            self.pending.add(selected)

    async def inspect(self, selected):
        candidate = self.candidates.get(selected)
        if candidate and not candidate.done():
            return
        if not self.eligible(selected):
            return
        call_id = await self.ringing(selected)
        if call_id is None:
            return
        self.candidates[selected] = asyncio.create_task(self.answer(selected, call_id))

    async def ringing(self, selected):
        """唯一一通 incoming 的 call_id；有其他通话或查询失败时返回 None。"""
        try:
            calls = await asyncio.wait_for(self.runtime.adapter.calls(selected), QUERY_SECONDS)
        except Exception:
            return None
        if len(calls) != 1 or calls[0].get('state') != 'incoming' or not isinstance(calls[0].get('id'), str):
            return None
        return calls[0]['id']

    async def answer(self, selected, call_id):
        adapter = self.runtime.adapter
        try:
            try:
                identity = await asyncio.wait_for(adapter.call_identity(selected, call_id), QUERY_SECONDS)
            except Exception:
                identity = None
            await asyncio.sleep(INBOUND_ANSWER_DELAY)
            # 等待期间来电消失、被本人接听、线路被占用或设备被隔离：放弃，不占线路。
            if not self.eligible(selected) or await self.ringing(selected) != call_id:
                return
            if identity is not None:
                try:
                    current = await asyncio.wait_for(adapter.call_identity(selected, call_id), QUERY_SECONDS)
                except Exception:
                    return
                if current != identity:
                    return
            await self.runtime.answer_inbound(selected, call_id)
        except asyncio.CancelledError:
            raise
        except GatewayError:
            pass    # 线路刚被占用或接听失败：会话自己收尾，来电由手机处理。
        except Exception:
            pass
        finally:
            if self.candidates.get(selected) is asyncio.current_task():
                self.candidates.pop(selected, None)

    def drop_candidate(self, selected):
        candidate = self.candidates.pop(selected, None)
        if candidate and not candidate.done():
            candidate.cancel()

    async def close(self):
        self.closed = True
        self.wake.set()
        for selected in tuple(self.subscriptions):
            with contextlib.suppress(Exception):
                self.subscriptions.pop(selected)()
        tasks = [task for task in self.candidates.values() if not task.done()]
        self.candidates.clear()
        if self.task:
            tasks.append(self.task)
            self.task.cancel()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
