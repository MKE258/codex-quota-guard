import unittest
from datetime import datetime

from quota_guard import QuotaController, QuotaState


class QuotaControllerTest(unittest.TestCase):
    def test_daily_limit_uses_remaining_quota_divided_by_remaining_days(self) -> None:
        state = QuotaState(
            remaining_quota=70,
            refresh_at="2026-06-08 12:00",
            day_key="2026-06-01",
            day_start_remaining=70,
        )
        controller = QuotaController(state)

        self.assertEqual(controller.remaining_days(datetime(2026, 6, 1, 12, 0)), 7)
        self.assertEqual(controller.daily_limit(datetime(2026, 6, 1, 12, 0)), 10)

    def test_usage_at_limit_should_pause(self) -> None:
        state = QuotaState(
            remaining_quota=70,
            refresh_at="2026-06-08 12:00",
            day_key="2026-06-01",
            day_start_remaining=70,
        )
        controller = QuotaController(state)
        now = datetime(2026, 6, 1, 12, 0)

        controller.log_usage(10, now)

        self.assertTrue(controller.should_pause(now))
        self.assertEqual(state.remaining_quota, 60)

    def test_refresh_restores_full_quota_and_moves_refresh_forward(self) -> None:
        state = QuotaState(
            remaining_quota=5,
            refresh_at="2026-06-01 12:00",
            day_key="2026-06-01",
            day_start_remaining=10,
            today_used=5,
        )
        controller = QuotaController(state)

        controller.normalize(datetime(2026, 6, 1, 12, 1))

        self.assertEqual(state.remaining_quota, 100)
        self.assertEqual(state.refresh_at, "2026-06-08 12:00")
        self.assertEqual(state.today_used, 0)

    def test_new_day_resets_daily_usage(self) -> None:
        state = QuotaState(
            remaining_quota=45,
            refresh_at="2026-06-08 12:00",
            day_key="2026-06-01",
            day_start_remaining=50,
            today_used=5,
        )
        controller = QuotaController(state)

        controller.normalize(datetime(2026, 6, 2, 9, 0))

        self.assertEqual(state.day_key, "2026-06-02")
        self.assertEqual(state.day_start_remaining, 45)
        self.assertEqual(state.today_used, 0)

    def test_remote_usage_decrease_counts_as_today_usage(self) -> None:
        state = QuotaState(
            remaining_quota=70,
            refresh_at="2026-06-08 12:00",
            day_key="2026-06-01",
            day_start_remaining=70,
        )
        controller = QuotaController(state)

        controller.sync_remote_usage(65, None, datetime(2026, 6, 1, 13, 0))

        self.assertEqual(state.remaining_quota, 65)
        self.assertEqual(state.today_used, 5)

    def test_remote_usage_increase_after_refresh_resets_today_usage(self) -> None:
        state = QuotaState(
            remaining_quota=5,
            refresh_at="2026-06-01 12:00",
            day_key="2026-06-01",
            day_start_remaining=10,
            today_used=5,
        )
        controller = QuotaController(state)

        controller.sync_remote_usage(
            100, datetime(2026, 6, 8, 12, 0), datetime(2026, 6, 1, 13, 0)
        )

        self.assertEqual(state.remaining_quota, 100)
        self.assertEqual(state.today_used, 0)


if __name__ == "__main__":
    unittest.main()
