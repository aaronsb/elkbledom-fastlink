"""Connection-management tests for BLEDOMInstance, run against a fake BLE stack.

Run: python3 -m unittest discover -s tests

Home Assistant, bleak and bleak-retry-connector are replaced by stubs, so the
tests need nothing installed. They check the properties whose absence leaked
Bluetooth connection slots and file descriptors until Home Assistant crashed:
one live connection per light, no pile-up of reconnect tasks while a light is
unreachable, and nothing left running after stop().
"""

import asyncio
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "custom_components" / "elkbledom_fastlink" / "elkbledom.py"


class FakeBleakError(Exception):
    pass


class FakeNotFound(FakeBleakError):
    pass


class FakeClient:
    def __init__(self, stack, disconnected_cb):
        self.stack = stack
        self.is_connected = True
        self._cb = disconnected_cb
        self.services = types.SimpleNamespace(get_characteristic=lambda uuid: "char")
        self.writes = []

    async def write_gatt_char(self, uuid, data, response):
        if not self.is_connected:
            raise FakeBleakError("not connected")
        self.writes.append(bytes(data))

    async def disconnect(self):
        if self.is_connected:
            self.is_connected = False
            self._cb(self)

    def drop(self):
        """The light went away."""
        self.is_connected = False
        self._cb(self)


class FakeStack:
    """Stands in for establish_connection: counts every connection opened."""

    def __init__(self):
        self.fail_next = 0
        self.connect_time = 0.01
        self.clients: list[FakeClient] = []
        self.attempts = 0

    @property
    def live(self):
        return [c for c in self.clients if c.is_connected]

    async def establish_connection(self, cls, device, name, disconnected_cb, **kwargs):
        self.attempts += 1
        await asyncio.sleep(self.connect_time)
        if self.fail_next:
            self.fail_next -= 1
            raise FakeBleakError("No backend with an available connection slot")
        client = FakeClient(self, disconnected_cb)
        self.clients.append(client)
        return client


def load_module(stack, state_dir):
    """Import elkbledom.py with its dependencies stubbed out."""
    bleak = types.ModuleType("bleak")
    bleak_exc = types.ModuleType("bleak.exc")
    bleak_exc.BleakDBusError = type("BleakDBusError", (FakeBleakError,), {})
    bleak_exc.BleakError = FakeBleakError
    bleak_backends = types.ModuleType("bleak.backends")
    bleak_service = types.ModuleType("bleak.backends.service")
    bleak_service.BleakGATTServiceCollection = object
    brc = types.ModuleType("bleak_retry_connector")
    brc.BleakClientWithServiceCache = FakeClient
    brc.BleakNotFoundError = FakeNotFound
    brc.BLEAK_RETRY_EXCEPTIONS = (FakeBleakError, asyncio.TimeoutError)
    brc.establish_connection = stack.establish_connection

    device = types.SimpleNamespace(name="ELK-BLEDWM 5C", address="BE:68:91:03:8B:17")
    ha = types.ModuleType("homeassistant")
    ha_components = types.ModuleType("homeassistant.components")
    ha_bt = types.ModuleType("homeassistant.components.bluetooth")
    ha_bt.async_ble_device_from_address = lambda hass, address, connectable=True: device
    ha_exc = types.ModuleType("homeassistant.exceptions")
    ha_exc.ConfigEntryNotReady = type("ConfigEntryNotReady", (Exception,), {})

    pkg = types.ModuleType("elkbledom_fastlink")
    pkg.__path__ = [str(MODULE_PATH.parent)]
    const = types.ModuleType("elkbledom_fastlink.const")
    const.DEFAULT_BRIGHTNESS_MODE = "auto"

    sys.modules.update({
        "bleak": bleak, "bleak.exc": bleak_exc, "bleak.backends": bleak_backends,
        "bleak.backends.service": bleak_service, "bleak_retry_connector": brc,
        "homeassistant": ha, "homeassistant.components": ha_components,
        "homeassistant.components.bluetooth": ha_bt, "homeassistant.exceptions": ha_exc,
        "elkbledom_fastlink": pkg, "elkbledom_fastlink.const": const,
    })
    spec = importlib.util.spec_from_file_location("elkbledom_fastlink.elkbledom", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.STATE_FILE = str(Path(state_dir) / "state.json")
    # Fast clocks so a test covers many heartbeats and backoffs in a second.
    for name, value in {"INITIAL_CONNECT_DELAY_SECONDS": 0.01, "HEARTBEAT_SECONDS": 0.05, "RECONNECT_MIN_SECONDS": 0.01,
                        "RECONNECT_MAX_SECONDS": 0.05, "BLEAK_BACKOFF_TIME": 0}.items():
        if hasattr(mod, name):
            setattr(mod, name, value)
    return mod


class FakeHass:
    def __init__(self):
        self.loop = asyncio.get_running_loop()

    def async_create_background_task(self, coro, name):
        return self.loop.create_task(coro, name=name)

    async def async_add_executor_job(self, func, *args):
        return await self.loop.run_in_executor(None, func, *args)


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stack = FakeStack()
        self.mod = load_module(self.stack, self.tmp.name)
        self.hass = FakeHass()
        self.baseline_tasks = len(asyncio.all_tasks())

    async def asyncTearDown(self):
        if hasattr(self, "light"):
            await self.light.stop()
        self.tmp.cleanup()

    def make_light(self):
        self.light = self.mod.BLEDOMInstance("BE:68:91:03:8B:17", False, 120, self.hass)
        return self.light

    def extra_tasks(self):
        return len(asyncio.all_tasks()) - self.baseline_tasks

    async def test_concurrent_commands_open_one_connection(self):
        light = self.make_light()
        self.stack.connect_time = 0.05
        await asyncio.gather(*(light.set_effect_speed(5) for _ in range(10)))
        self.assertEqual(len(self.stack.live), 1, "exactly one live connection")
        self.assertEqual(len(self.stack.clients), 1, "no connection opened and abandoned")

    async def test_unreachable_light_does_not_pile_up_tasks(self):
        self.stack.fail_next = 10**6  # unreachable
        light = self.make_light()
        await asyncio.sleep(1)  # many heartbeats and backoffs
        tasks_while_down = self.extra_tasks()
        self.assertLessEqual(tasks_while_down, 2, f"{tasks_while_down} tasks for one light")

        self.stack.fail_next = 0  # light comes back
        await asyncio.sleep(0.5)
        self.assertEqual(len(self.stack.live), 1, "one live connection after recovery")
        self.assertEqual(len(self.stack.clients), 1, "no extra connections opened on recovery")

    async def test_drop_reconnects_once(self):
        light = self.make_light()
        await light.set_effect_speed(5)
        self.stack.live[0].drop()
        await asyncio.sleep(0.5)
        self.assertEqual(len(self.stack.live), 1)
        self.assertEqual(len(self.stack.clients), 2, "one reconnect, not several")

    async def test_stop_leaves_nothing_running(self):
        light = self.make_light()
        await light.set_effect_speed(5)
        await light.stop()
        await asyncio.sleep(0.2)
        self.assertEqual(self.stack.live, [], "link closed")
        self.assertEqual(self.extra_tasks(), 0, "no task outlives stop()")
        del self.light


if __name__ == "__main__":
    unittest.main()
