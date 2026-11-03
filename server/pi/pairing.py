"""BlueZ application agent. Reject requests outside its active target."""
from dbus_next.service import ServiceInterface, method
from dbus_next.errors import DBusError


class Agent(ServiceInterface):
    def __init__(self, owner):
        super().__init__("org.bluez.Agent1")
        self.owner = owner

    async def ask(self, path, kind, code=None):
        for key, session in self.owner.pairings.sessions.items():
            if session["state"] != "connecting":
                continue
            if await self.owner.device_path(session["address"]) == path:
                try:
                    return await self.owner.pairings.ask(key, session["address"], kind, code)
                except Exception:
                    break
        raise DBusError("org.bluez.Error.Rejected", "Pairing request rejected")

    @method()
    def Release(self):
        pass

    @method()
    async def RequestConfirmation(self, device: 'o', passkey: 'u'):
        if not await self.ask(device, "confirmation", f"{passkey:06d}"):
            raise DBusError("org.bluez.Error.Rejected", "Pairing rejected")

    @method()
    async def RequestPasskey(self, device: 'o') -> 'u':
        return int(await self.ask(device, "passkey"))

    @method()
    async def RequestPinCode(self, device: 'o') -> 's':
        return await self.ask(device, "pin")

    @method()
    def RequestAuthorization(self, device: 'o'):
        raise DBusError("org.bluez.Error.Rejected", "Explicit pairing required")

    @method()
    def AuthorizeService(self, device: 'o', uuid: 's'):
        raise DBusError("org.bluez.Error.Rejected", "No unsolicited authorization")

    @method()
    async def Cancel(self):
        for key in tuple(self.owner.pairings.work):
            await self.owner.pairings.cancel(key)
