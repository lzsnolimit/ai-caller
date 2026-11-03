import asyncio
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from bumble import hci
from bumble.keys import PairingKeys

from server.common.storage import Store
from server.mac.native.acipc_pair import SelectedKeyStore

CONTROLLER = hci.Address('11:22:33:44:55:66')
PHONE = hci.Address('AA:BB:CC:DD:EE:01', hci.Address.PUBLIC_DEVICE_ADDRESS)
OTHER = hci.Address('AA:BB:CC:DD:EE:02', hci.Address.PUBLIC_DEVICE_ADDRESS)


def keys(value=bytes(range(16)), key_type=8, authenticated=True):
    return PairingKeys(link_key=PairingKeys.Key(value, authenticated), link_key_type=key_type)


class PairKeysFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.path = Path(self.temp.name) / 'devices' / 'pair-keys.json'

    def tearDown(self):
        self.temp.cleanup()

    def keystore(self, phone=PHONE):
        return SelectedKeyStore(CONTROLLER, phone, self.store)

    def test_saved_key_survives_restart_in_private_file(self):
        asyncio.run(self.keystore().update(str(PHONE), keys()))
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        loaded = asyncio.run(self.keystore().get(str(PHONE)))
        self.assertEqual(bytes(loaded.link_key.value), bytes(range(16)))
        self.assertEqual(loaded.link_key_type, 8)
        self.assertTrue(loaded.link_key.authenticated)

    def test_each_phone_has_its_own_entry_and_delete_keeps_others(self):
        asyncio.run(self.keystore().update(str(PHONE), keys()))
        asyncio.run(self.keystore(OTHER).update(str(OTHER), keys(bytes(16))))
        asyncio.run(self.keystore().delete())
        self.assertIsNone(asyncio.run(self.keystore().get(str(PHONE))))
        self.assertIsNotNone(asyncio.run(self.keystore(OTHER).get(str(OTHER))))
        self.assertEqual(len(json.loads(self.path.read_text())), 1)

    def test_out_of_scope_or_unauthenticated_keys_are_refused(self):
        self.assertIsNone(asyncio.run(self.keystore().get(str(OTHER))))
        with self.assertRaises(RuntimeError):
            asyncio.run(self.keystore().update(str(OTHER), keys()))
        with self.assertRaises(RuntimeError):
            asyncio.run(self.keystore().update(str(PHONE), keys(authenticated=False)))
        self.assertFalse(self.path.exists())

    def test_missing_file_means_not_paired(self):
        self.assertIsNone(asyncio.run(self.keystore().get(str(PHONE))))
        asyncio.run(self.keystore().delete())


if __name__ == '__main__':
    unittest.main()
