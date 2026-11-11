"""Kiểm tra giới hạn thực thi tool khi liên tục bị rate limit."""

import unittest

from agent import run_agent
from test_lab import FakeProvider, tool_call, final


class RateLimitedTools:
    def __init__(self):
        self.execution_count = 0

    def execute(self, name, arguments):
        self.execution_count += 1
        return {
            "ok": False,
            "error": "RATE_LIMITED",
            "http_status": 429,
            "retry_after_seconds": 0,
            "retriable": True,
        }


class RetryBudgetTests(unittest.TestCase):
    def test_persistent_429_limits_actual_tool_executions(self):
        tools = RateLimitedTools()

        # Cố tình mô phỏng model yêu cầu cùng tool bốn lần.
        responses = [
            tool_call(
                f"call-{i}",
                "get_status",
                '{"provider":"github"}',
            )
            for i in range(4)
        ]
        responses.append(final("Không xác nhận được trạng thái."))

        provider = FakeProvider(responses)
        result = run_agent(
            "Kiểm tra GitHub",
            provider,
            tools,
            max_steps=6,
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(tools.execution_count, 3)


if __name__ == "__main__":
    unittest.main()
