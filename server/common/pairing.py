"""Pairing responses belong to one device and one live request only."""
import asyncio
import re
import secrets
import time

from .contracts import GatewayError, address


class Pairings:
    def __init__(self, adapter):
        self.adapter = adapter
        self.sessions = {}
        self.work = {}
        adapter.pairings = self
        self.on_change = lambda: None

    def get(self, key):
        session = self.sessions.get(key)
        if not session:
            raise GatewayError("pairing_not_found", 404)
        return {k: v for k, v in session.items() if k not in ("future", "deadline")}

    async def start(self, selected):
        selected = address(selected)
        if any(s["address"] == selected and s["state"] in ("connecting", "waiting") for s in self.sessions.values()):
            raise GatewayError("pairing_busy", 409)
        key = secrets.token_hex(16)
        self.sessions[key] = {"id": key, "address": selected, "state": "connecting"}
        self.on_change()

        async def pair():
            try:
                await self.adapter.pair(selected, key)
                self.sessions[key]["state"] = "paired"
            except asyncio.CancelledError:
                self.sessions[key]["state"] = "cancelled"
                raise
            except Exception:
                session = self.sessions[key]
                if session["state"] not in ("expired", "cancelled"):
                    session["state"] = "failed"
                session["error"] = "pairing_failed"
            finally:
                self.work.pop(key, None)
                self.on_change()
        self.work[key] = asyncio.create_task(pair())
        return self.get(key)

    async def ask(self, key, selected, kind, code=None):
        session = self.sessions.get(key)
        if not session or session["address"] != address(selected) or session["state"] != "connecting":
            raise GatewayError("pairing_cancelled", 409)
        future = asyncio.get_running_loop().create_future()
        session.update(state="waiting", kind=kind, code=code, future=future, deadline=time.monotonic() + 40)
        self.on_change()
        try:
            result = await asyncio.wait_for(future, 40)
            session["state"] = "connecting"
            return result
        except asyncio.TimeoutError:
            session["state"] = "expired"
            raise GatewayError("pairing_expired", 409) from None
        finally:
            session.pop("future", None)
            session.pop("deadline", None)
            self.on_change()

    def respond(self, key, body):
        session = self.sessions.get(key)
        if not session or body.get("address") != session["address"]:
            raise GatewayError("pairing_mismatch", 409)
        if session["state"] != "waiting" or time.monotonic() >= session["deadline"] or session["future"].done():
            raise GatewayError("pairing_expired", 409)
        if session["kind"] == "confirmation":
            value = body.get("accept")
            if not isinstance(value, bool):
                raise GatewayError("confirmation_required")
        else:
            value = body.get("code")
            pattern = r"[0-9]{6}" if session["kind"] == "passkey" else r"[ -~]{1,16}"
            if not isinstance(value, str) or not re.fullmatch(pattern, value):
                raise GatewayError("invalid_pairing_code")
        session["future"].set_result(value)

    async def cancel(self, key):
        self.get(key)
        task = self.work.get(key)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def close(self):
        for task in tuple(self.work.values()):
            task.cancel()
        await asyncio.gather(*tuple(self.work.values()), return_exceptions=True)
