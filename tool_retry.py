"""Giới hạn retry cho status tools."""

import math
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


class ToolRetryBudget:
    def __init__(self):
        self.states = {}
        self.wait_remaining = 10.0

    def _retry_delay(self, observation, failures):
        raw = observation.get("retry_after_seconds")
        if raw is None:
            raw = observation.get("retry_after")

        if raw is None:
            # Không có Retry-After: backoff hữu hạn.
            return float(2 ** (failures - 1))

        try:
            delay = float(raw)
        except (TypeError, ValueError):
            try:
                when = parsedate_to_datetime(str(raw))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                delay = (when - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                return None

        return max(0.0, delay) if math.isfinite(delay) else None

    def execute(self, tools, name, arguments):
        # Không để model đổi call_id để vượt giới hạn.
        target = str(arguments.get("provider", name))
        key = (name, target)
        state = self.states.setdefault(
            key,
            {"failures": 0, "blocked": False, "ready_at": 0.0},
        )

        if state["blocked"]:
            return {
                "ok": False,
                "error": "RETRY_LIMIT",
                "retry_allowed": False,
                "message": "Dừng gọi tool này; báo phần chưa kiểm tra được.",
            }

        wait = max(0.0, state["ready_at"] - time.monotonic())
        if wait > 0:
            # Chỉ chờ khi model thực sự yêu cầu thử lại.
            time.sleep(wait)
            self.wait_remaining -= wait

        observation = tools.execute(name, arguments)

        if not isinstance(observation, dict):
            return {"ok": False, "error": "INVALID_TOOL_RESULT"}

        is_rate_limited = (
            observation.get("error") == "RATE_LIMITED"
            or observation.get("http_status") == 429
        )

        if not is_rate_limited:
            state["failures"] = 0
            state["ready_at"] = 0.0
            return observation

        observation = dict(observation)
        state["failures"] += 1
        delay = self._retry_delay(observation, state["failures"])

        allowed = (
            state["failures"] < 3
            and delay is not None
            and delay <= 5.0
            and delay <= self.wait_remaining
        )

        observation["retry_allowed"] = allowed
        observation["rate_limit_count"] = state["failures"]

        if allowed:
            state["ready_at"] = time.monotonic() + delay
        else:
            state["blocked"] = True
            observation["message"] = (
                "Đã hết budget retry hoặc thời gian chờ vượt budget. "
                "Dừng truy vấn này và báo kết quả từng phần."
            )

        return observation
