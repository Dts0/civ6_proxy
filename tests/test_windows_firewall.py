import ipaddress
import subprocess
import unittest
from unittest.mock import patch

from civ6relay.windows import WindowsPlatformError, ensure_game_firewall_rule


class FirewallRuleTests(unittest.TestCase):
    @patch("civ6relay.windows.subprocess.run")
    def test_uses_entire_selected_subnet_after_network_change(self, run):
        run.return_value.returncode = 0
        for cidr in ("10.250.250.0/24", "172.31.250.0/24"):
            ensure_game_firewall_rule(ipaddress.IPv4Network(cidr))
            script = run.call_args.args[0][-1]
            self.assertIn("Name = 'Civ6Relay-Game-Inbound'", script)
            self.assertIn(f"RemoteAddress = '{cidr}'", script)
            self.assertIn("Profile = 'Any'", script)
            self.assertIn("LocalPort = '62056', '62900-62999'", script)
            self.assertNotIn("LocalAddress", script)

    @patch("civ6relay.windows.subprocess.run")
    def test_surfaces_policy_failure_and_timeout(self, run):
        subnet = ipaddress.IPv4Network("192.168.250.0/24")
        run.return_value = subprocess.CompletedProcess([], 1, stderr="Access denied")
        with self.assertRaisesRegex(WindowsPlatformError, "Access denied"):
            ensure_game_firewall_rule(subnet)
        run.side_effect = subprocess.TimeoutExpired("powershell.exe", 20)
        with self.assertRaises(WindowsPlatformError):
            ensure_game_firewall_rule(subnet)
