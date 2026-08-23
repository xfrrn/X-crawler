import unittest
from html.parser import HTMLParser
from pathlib import Path


class AdminUiSmokeTest(unittest.TestCase):
    def test_workbench_routes_and_safe_dialogs_are_present(self) -> None:
        static_dir = Path(__file__).parents[1] / "app" / "static"
        html = (static_dir / "index.html").read_text(encoding="utf-8")
        js = (static_dir / "app.js").read_text(encoding="utf-8")

        HTMLParser().feed(html)
        for route in ("overview", "monitors", "pmonitors", "tweets", "pposts", "accounts", "stats"):
            self.assertIn(f'key: "{route}"', js)
        for dialog in ("monitorDialog", "platformMonitorDialog", "confirmDialog"):
            self.assertIn(f'ref="{dialog}"', html)
        self.assertNotIn("confirm(", js)
        self.assertNotIn("alert(", js)


if __name__ == "__main__":
    unittest.main()
