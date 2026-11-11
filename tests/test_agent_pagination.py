"""Kiểm tra agent với timeline phân trang, không gọi LLM thật."""

import json
import unittest

from agent import run_agent
from bonus.tools import BonusTools
from bonus.test_agent import call, final


class PaginationProvider:
    def __init__(self):
        self.step = 0
        self.pages = []

    def complete(self, messages, schemas):
        self.step += 1

        if self.step == 1:
            return call(
                "page-1",
                "get_incident_details",
                {
                    "provider": "vercel",
                    "incident_id": "sps6n39gm4sg",
                    "offset": 0,
                    "limit": 2,
                },
            )

        if self.step == 2:
            first_page = json.loads(messages[-1]["content"])
            self.pages.append(first_page)

            return call(
                "page-2",
                "get_incident_details",
                {
                    "provider": "vercel",
                    "incident_id": first_page["id"],
                    "offset": first_page["next_offset"],
                    "limit": 2,
                },
            )

        if self.step == 3:
            second_page = json.loads(messages[-1]["content"])
            self.pages.append(second_page)

            return final(
                "Đã đọc hai trang, chưa đọc hết timeline. "
                "Nguồn: " + second_page["url"]
            )

        raise AssertionError("Agent gọi model ngoài dự kiến.")


class AgentPaginationTests(unittest.TestCase):
    def test_reads_next_page_and_preserves_evidence(self):
        provider = PaginationProvider()
        result = run_agent(
            "Đọc timeline incident Vercel.",
            provider,
            BonusTools(),
            max_steps=5,
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(provider.pages), 2)

        first, second = provider.pages
        self.assertEqual(first["next_offset"], 2)
        self.assertEqual(second["offset"], 2)
        self.assertEqual(second["next_offset"], 4)
        self.assertEqual(first["total_updates"], 7)

        first_times = {update["created_at"] for update in first["updates"]}
        second_times = {update["created_at"] for update in second["updates"]}
        self.assertTrue(first_times.isdisjoint(second_times))

        second_page_text = " ".join(update["body"] for update in second["updates"])
        self.assertIn("github", second_page_text.lower())

        tool_events = [event for event in result["trace"] if event["event"] == "tool"]
        self.assertEqual(
            [event["arguments"]["offset"] for event in tool_events],
            [0, 2],
        )
        self.assertIn(second["url"], result["answer"])


if __name__ == "__main__":
    unittest.main()
