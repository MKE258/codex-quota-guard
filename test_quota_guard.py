import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from quota_guard import (
    QuotaController,
    QuotaState,
    format_process_error,
    reader_env,
    resolve_state_file,
)


class QuotaControllerTest(unittest.TestCase):
    def test_reader_env_enables_system_chrome_profile(self) -> None:
        env = reader_env(True, {"PATH": "example"})

        self.assertEqual(env["CODEX_QUOTA_GUARD_PROFILE_MODE"], "system-chrome")
        self.assertEqual(env["PATH"], "example")

    def test_reader_env_removes_system_chrome_profile_when_disabled(self) -> None:
        env = reader_env(False, {"CODEX_QUOTA_GUARD_PROFILE_MODE": "system-chrome"})

        self.assertNotIn("CODEX_QUOTA_GUARD_PROFILE_MODE", env)

    def test_process_error_falls_back_when_stderr_is_missing(self) -> None:
        self.assertEqual(
            format_process_error(None, "网页同步失败\n"),
            "网页同步失败",
        )

    def test_process_error_uses_default_when_no_output_exists(self) -> None:
        self.assertEqual(
            format_process_error(None, None),
            "网页额度同步失败。",
        )

    def test_state_file_prefers_local_app_data_directory(self) -> None:
        app_dir = Path("C:/Program Files/CodexQuotaGuard")
        state_file = resolve_state_file(
            app_dir,
            {"LOCALAPPDATA": "C:/Users/Admin/AppData/Local"},
        )

        self.assertEqual(
            state_file,
            Path("C:/Users/Admin/AppData/Local/CodexQuotaGuard/quota_guard_state.json"),
        )

    def test_state_load_migrates_legacy_state_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir) / "app"
            data_dir = Path(temp_dir) / "local" / "CodexQuotaGuard"
            app_dir.mkdir()
            legacy_file = app_dir / "quota_guard_state.json"
            state_file = data_dir / "quota_guard_state.json"
            legacy_file.write_text(
                '{"remaining_quota": 42, "refresh_at": "2026-06-08 12:00"}',
                encoding="utf-8",
            )

            state = QuotaState.load_from_files(state_file, legacy_file)

            self.assertEqual(state.remaining_quota, 42)
            self.assertTrue(state_file.exists())
            self.assertFalse(legacy_file.exists())

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
            last_sync_at="2026-06-01 12:00",
        )
        controller = QuotaController(state)

        controller.sync_remote_usage(65, None, datetime(2026, 6, 1, 13, 0))

        self.assertEqual(state.remaining_quota, 65)
        self.assertEqual(state.today_used, 5)

    def test_first_remote_sync_establishes_baseline_without_counting_usage(self) -> None:
        state = QuotaState(
            remaining_quota=100,
            refresh_at="2026-06-08 12:00",
            day_key="2026-06-01",
            day_start_remaining=100,
        )
        controller = QuotaController(state)

        controller.sync_remote_usage(73, None, datetime(2026, 6, 1, 13, 0))

        self.assertEqual(state.remaining_quota, 73)
        self.assertEqual(state.day_start_remaining, 73)
        self.assertEqual(state.today_used, 0)

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
