import tempfile
import unittest
from pathlib import Path

from civ6relay.config import ClientConfig, load_config, parse_relay_address, save_config


class ConfigTests(unittest.TestCase):
    def test_parse_relay_address(self):
        self.assertEqual(parse_relay_address("relay.example:5000"), ("relay.example", 5000))
        self.assertEqual(parse_relay_address("relay.example"), ("relay.example", 42056))
        with self.assertRaises(ValueError):
            parse_relay_address("relay.example:nope")

    def test_config_round_trip_has_no_session_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            expected = ClientConfig("relay.example", 42056, "Player")
            save_config(expected, path)
            self.assertEqual(load_config(path), expected)
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("token", text.lower())
            self.assertNotIn("room", text.lower())
