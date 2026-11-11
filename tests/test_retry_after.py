import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from tool_retry import ToolRetryBudget


class ScriptedTools:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.execution_count = 0

    def execute(self, name, arguments):
        self.execution_count += 1
        return next(self.responses)


class RetryAfterTests(unittest.TestCase):
    def test_waits_requested_seconds_before_retry(self):
        tools = ScriptedTools(
            [
                {
                    "ok": False,
                    "error": "RATE_LIMITED",
                    "retry_after": "2",
                },
                {"ok": True},
            ]
        )
        budget = ToolRetryBudget()

        with (
            patch("tool_retry.time.monotonic", return_value=100.0),
            patch("tool_retry.time.sleep") as sleep,
        ):
            first = budget.execute(tools, "get_status", {"provider": "github"})
            sleep.assert_not_called()
            self.assertTrue(first["retry_allowed"])

            second = budget.execute(tools, "get_status", {"provider": "github"})

        sleep.assert_called_once_with(2.0)
        self.assertTrue(second["ok"])
        self.assertEqual(tools.execution_count, 2)
        self.assertEqual(budget.wait_remaining, 8.0)

    def test_long_wait_blocks_retry_without_sleeping(self):
        tools = ScriptedTools(
            [
                {
                    "ok": False,
                    "error": "RATE_LIMITED",
                    "retry_after": "120",
                },
            ]
        )
        budget = ToolRetryBudget()

        with patch("tool_retry.time.sleep") as sleep:
            first = budget.execute(tools, "get_status", {"provider": "github"})
            second = budget.execute(tools, "get_status", {"provider": "github"})

        self.assertFalse(first["retry_allowed"])
        self.assertEqual(second["error"], "RETRY_LIMIT")
        self.assertEqual(tools.execution_count, 1)
        sleep.assert_not_called()

    def test_parses_http_date(self):
        fixed_now = datetime(2026, 11, 11, 12, 0, 0, tzinfo=timezone.utc)
        budget = ToolRetryBudget()

        with patch("tool_retry.datetime") as clock:
            clock.now.return_value = fixed_now
            delay = budget._retry_delay(
                {"retry_after": "Wed, 11 Nov 2026 12:00:04 GMT"},
                failures=1,
            )

        self.assertEqual(delay, 4.0)


if __name__ == "__main__":
    unittest.main()
