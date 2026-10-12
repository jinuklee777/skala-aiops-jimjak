import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from collector.config import KST, REPO_ROOT, load_config, parse_time


class ConfigTest(unittest.TestCase):
    def test_environment_overrides_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text("# 주석\nexport DATA_GO_KR_SERVICE_KEY='from-file'\n"
                                "COLLECTOR_INTERVAL_MINUTES=5\n", encoding="utf-8")
            cfg = load_config({"COLLECTOR_ENV_FILE": str(env_file), "COLLECTOR_INTERVAL_MINUTES": "7",
                               "COLLECTOR_DATA_DIR": "var/collector"})
        self.assertEqual(cfg.service_key, "from-file")
        self.assertEqual(cfg.interval_minutes, 7)
        self.assertEqual(cfg.data_dir, REPO_ROOT / "var/collector")

    def test_rejects_bad_values(self):
        for name, value in (("COLLECTOR_INTERVAL_MINUTES", "0"), ("COLLECTOR_NUM_OF_ROWS", "many")):
            with self.subTest(name=name), self.assertRaises(ValueError):
                load_config({"COLLECTOR_ENV_FILE": "/nonexistent/.env", name: value})

    def test_parse_time(self):
        expected = datetime(2026, 10, 12, 14, 0, tzinfo=KST)
        for text in ("2026-10-12 14:00", "202610121400", "2026-10-12T05:00:00+00:00"):
            with self.subTest(text=text):
                self.assertEqual(parse_time(text), expected)
