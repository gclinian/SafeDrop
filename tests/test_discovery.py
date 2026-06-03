"""Regression tests for :mod:`safedrop.discovery`.

We don't exercise the full UDP loop here. These tests lock in the
broadcast-target derivation, which previously broke on networks where
the active LAN is not a /24, such as iPhone Personal Hotspot.
"""

from __future__ import annotations

import socket
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import safedrop.discovery as discovery  # noqa: E402
from safedrop.discovery import BroadcastInterface  # noqa: E402


class BroadcastInterfacesTest(unittest.TestCase):
    def test_computes_iphone_hotspot_broadcast_from_netmask(self) -> None:
        self.assertEqual(
            discovery._broadcast_for("172.20.10.6", "255.255.255.240"),
            "172.20.10.15",
        )

    def test_skips_down_adapters_and_unusable_ipv4_addresses(self) -> None:
        addrs = {
            "Wi-Fi": [
                SimpleNamespace(
                    family=socket.AF_INET,
                    address="172.20.10.6",
                    netmask="255.255.255.240",
                    broadcast=None,
                )
            ],
            "Ethernet": [
                SimpleNamespace(
                    family=socket.AF_INET,
                    address="192.168.50.10",
                    netmask="255.255.255.0",
                    broadcast=None,
                )
            ],
            "Loopback": [
                SimpleNamespace(
                    family=socket.AF_INET,
                    address="127.0.0.1",
                    netmask="255.0.0.0",
                    broadcast=None,
                )
            ],
        }
        stats = {
            "Wi-Fi": SimpleNamespace(isup=True),
            "Ethernet": SimpleNamespace(isup=False),
            "Loopback": SimpleNamespace(isup=True),
        }

        with (
            mock.patch.object(discovery.psutil, "net_if_addrs", return_value=addrs),
            mock.patch.object(discovery.psutil, "net_if_stats", return_value=stats),
        ):
            self.assertEqual(
                discovery._broadcast_interfaces(),
                (BroadcastInterface(name="Wi-Fi", ip="172.20.10.6", broadcast="172.20.10.15"),),
            )


class BroadcastTargetsTest(unittest.TestCase):
    def test_includes_loopback_directed_and_global_targets(self) -> None:
        interfaces = (
            BroadcastInterface(name="Radmin", ip="26.216.2.29", broadcast="26.255.255.255"),
            BroadcastInterface(name="Wi-Fi", ip="172.20.10.6", broadcast="172.20.10.15"),
        )
        with mock.patch.object(discovery, "_broadcast_interfaces", return_value=interfaces):
            targets = discovery._broadcast_targets("172.20.10.6")

        self.assertIn("127.0.0.1", targets)
        self.assertIn("172.20.10.15", targets)
        self.assertIn("255.255.255.255", targets)
        self.assertNotIn("172.20.10.255", targets)
        self.assertNotIn("26.255.255.255", targets)

    def test_loopback_first(self) -> None:
        interfaces = (
            BroadcastInterface(name="Wi-Fi", ip="192.168.1.10", broadcast="192.168.1.255"),
        )
        with mock.patch.object(discovery, "_broadcast_interfaces", return_value=interfaces):
            self.assertEqual(discovery._broadcast_targets("192.168.1.10")[0], "127.0.0.1")

    def test_handles_malformed_ip_without_synthesizing_broadcast(self) -> None:
        interfaces = (
            BroadcastInterface(name="Wi-Fi", ip="192.168.1.10", broadcast="192.168.1.255"),
        )
        with mock.patch.object(discovery, "_broadcast_interfaces", return_value=interfaces):
            targets = discovery._broadcast_targets("not-an-ip")

        self.assertIn("127.0.0.1", targets)
        self.assertIn("255.255.255.255", targets)
        self.assertNotIn("not-an-ip", targets)
        self.assertNotIn("not.an.ip.255", targets)

    def test_no_duplicates(self) -> None:
        interfaces = (
            BroadcastInterface(name="Wi-Fi", ip="192.168.1.10", broadcast="192.168.1.255"),
            BroadcastInterface(name="Wi-Fi 2", ip="192.168.1.11", broadcast="192.168.1.255"),
        )
        with mock.patch.object(discovery, "_broadcast_interfaces", return_value=interfaces):
            targets = discovery._broadcast_targets()

        self.assertEqual(len(targets), len(set(targets)))


class BroadcastEndpointsTest(unittest.TestCase):
    def test_binds_directed_broadcasts_to_each_source_ip(self) -> None:
        interfaces = (
            BroadcastInterface(name="Radmin", ip="26.216.2.29", broadcast="26.255.255.255"),
            BroadcastInterface(name="Wi-Fi", ip="172.20.10.6", broadcast="172.20.10.15"),
        )
        with mock.patch.object(discovery, "_broadcast_interfaces", return_value=interfaces):
            endpoints = discovery._broadcast_endpoints()

        self.assertIn(("127.0.0.1", None), endpoints)
        self.assertIn(("172.20.10.15", "172.20.10.6"), endpoints)
        self.assertIn(("255.255.255.255", "172.20.10.6"), endpoints)
        self.assertNotIn(("172.20.10.15", "26.216.2.29"), endpoints)


class LocalIPDisplayTest(unittest.TestCase):
    def test_displays_all_active_interface_ips(self) -> None:
        interfaces = (
            BroadcastInterface(name="Radmin", ip="26.216.2.29", broadcast="26.255.255.255"),
            BroadcastInterface(name="Wi-Fi", ip="172.20.10.6", broadcast="172.20.10.15"),
        )
        with mock.patch.object(discovery, "_broadcast_interfaces", return_value=interfaces):
            self.assertEqual(discovery._local_ip_display(), "26.216.2.29, 172.20.10.6")


if __name__ == "__main__":
    unittest.main(verbosity=2)
