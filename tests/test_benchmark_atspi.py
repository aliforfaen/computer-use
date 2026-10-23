"""Focused offline tests for the virtual AT-SPI startup compatibility fix.

No D-Bus, no desktop and no provider: a fake ``dbus`` module exercises the
old and new ``org.a11y.Status`` interfaces and the failure paths. See
docs/34-jev-study-bugfix-handoff.md.
"""

from __future__ import annotations

import sys
import types
import unittest
from unittest import mock

from jevdesktop import benchmark_capture as bc


class FakeDBusException(Exception):
    pass


class FakeProps:
    """Fake org.freedesktop.DBus.Properties over one discoverable interface."""

    def __init__(self, available, *, fail_set=(), ignore_sets=False):
        self.available = dict(available)
        self.fail_set = set(fail_set)
        self.ignore_sets = ignore_sets
        self.set_calls: list[str] = []
        self.getall_calls = 0

    def GetAll(self, interface):
        self.getall_calls += 1
        return dict(self.available)

    def Set(self, interface, name, value):
        if name in self.fail_set:
            raise FakeDBusException("write denied")
        if name not in self.available:
            raise FakeDBusException("no such property")
        self.set_calls.append(name)
        if not self.ignore_sets:
            self.available[name] = bool(value)


class FakeBus:
    def __init__(self, props):
        self.props = props
        self.closed = False
        self.get_object_calls: list[tuple] = []

    def get_object(self, name, path):
        self.get_object_calls.append((name, path))
        return object()

    def close(self):
        self.closed = True


class FakeEngine:
    def __init__(self, address="unix:path=/tmp/isolated-session"):
        self.address = address

    def _get_session(self):
        return types.SimpleNamespace(info=types.SimpleNamespace(dbus_address=self.address))


def _fake_dbus(props, *, bus_created: list, fail_connect: bool = False):
    module = types.ModuleType("dbus")
    module.DBusException = FakeDBusException
    module.Boolean = bool

    def BusConnection(address):
        if fail_connect:
            raise FakeDBusException("no session bus")
        bus = FakeBus(props)
        bus_created.append(bus)
        return bus

    module.bus = types.SimpleNamespace(BusConnection=BusConnection)
    module.Interface = lambda obj, name: props
    return module


class EnableVirtualAtspiTests(unittest.TestCase):
    def _run(self, props, **kwargs):
        created: list[FakeBus] = []
        module = _fake_dbus(props, bus_created=created, **kwargs)
        with mock.patch.dict(sys.modules, {"dbus": module}):
            return bc._enable_virtual_atspi(FakeEngine()), created

    def test_new_interface_sets_only_is_enabled(self):
        # at-spi2-core 2.62: ScreenReaderEnabled is gone. It must not be written.
        props = FakeProps({"IsEnabled": False})
        result, created = self._run(props)
        self.assertEqual(result, {"IsEnabled": True})
        self.assertEqual(props.set_calls, ["IsEnabled"])
        self.assertTrue(created and created[0].closed)

    def test_old_interface_still_enables_both_flags(self):
        props = FakeProps({"IsEnabled": False, "ScreenReaderEnabled": False})
        result, created = self._run(props)
        self.assertEqual(result, {"IsEnabled": True, "ScreenReaderEnabled": True})
        self.assertEqual(props.set_calls, ["IsEnabled", "ScreenReaderEnabled"])
        self.assertTrue(created and created[0].closed)

    def test_missing_required_property_is_reported_and_bus_closed(self):
        props = FakeProps({"ScreenReaderEnabled": False})
        with self.assertRaises(bc.CaptureError) as caught:
            self._run(props)
        self.assertIn("IsEnabled", str(caught.exception))
        self.assertEqual(props.set_calls, [])

    def test_write_failure_is_not_swallowed_and_bus_closed(self):
        props = FakeProps({"IsEnabled": False}, fail_set={"IsEnabled"})
        created: list[FakeBus] = []
        module = _fake_dbus(props, bus_created=created)
        with mock.patch.dict(sys.modules, {"dbus": module}):
            with self.assertRaises(bc.CaptureError) as caught:
                bc._enable_virtual_atspi(FakeEngine())
        self.assertIn("DBusException", str(caught.exception))
        self.assertTrue(created and created[0].closed)

    def test_read_back_failure_is_reported(self):
        props = FakeProps({"IsEnabled": False}, ignore_sets=True)
        with self.assertRaises(bc.CaptureError) as caught:
            self._run(props)
        self.assertIn("did not enable", str(caught.exception))

    def test_connect_failure_is_reported_without_leaking_detail(self):
        props = FakeProps({"IsEnabled": False})
        created: list[FakeBus] = []
        module = _fake_dbus(props, bus_created=created, fail_connect=True)
        with mock.patch.dict(sys.modules, {"dbus": module}):
            with self.assertRaises(bc.CaptureError) as caught:
                bc._enable_virtual_atspi(FakeEngine())
        self.assertIn("DBusException", str(caught.exception))
        self.assertEqual(created, [])

    def test_missing_dbus_python_is_reported(self):
        with mock.patch.dict(sys.modules, {"dbus": None}):
            with self.assertRaises(bc.CaptureError) as caught:
                bc._enable_virtual_atspi(FakeEngine())
        self.assertIn("dbus-python", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
