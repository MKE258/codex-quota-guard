from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
STATE_FILE = APP_DIR / "quota_guard_state.json"
CHECK_INTERVAL_MS = 1000
AUTO_SYNC_INTERVAL_SECONDS = 300
READER_SCRIPT = APP_DIR / "codex_usage_reader.js"
NODE_EXECUTABLE = APP_DIR / "node.exe" if (APP_DIR / "node.exe").exists() else "node"


def parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.strip())


def format_datetime(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M")


@dataclass
class QuotaState:
    command: str = ""
    project_dir: str = ""
    remaining_quota: float = 100.0
    refresh_at: str = ""
    day_key: str = ""
    day_start_remaining: float = 100.0
    today_used: float = 0.0
    auto_sync: bool = False
    last_sync_at: str = ""

    @classmethod
    def load(cls) -> "QuotaState":
        if not STATE_FILE.exists():
            return cls()
        try:
            return cls(**json.loads(STATE_FILE.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return cls()

    def save(self) -> None:
        STATE_FILE.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


class QuotaController:
    def __init__(self, state: QuotaState) -> None:
        self.state = state

    def normalize(self, now: datetime) -> None:
        refresh_at = parse_datetime(self.state.refresh_at)
        refreshed = False
        while now >= refresh_at:
            refresh_at += timedelta(days=7)
            refreshed = True
        if refreshed:
            self.state.refresh_at = format_datetime(refresh_at)
            self.state.remaining_quota = 100.0
            self.state.day_key = now.date().isoformat()
            self.state.day_start_remaining = 100.0
            self.state.today_used = 0.0

        today = now.date().isoformat()
        if self.state.day_key != today:
            self.state.day_key = today
            self.state.day_start_remaining = self.state.remaining_quota
            self.state.today_used = 0.0

    def remaining_days(self, now: datetime) -> int:
        refresh_at = parse_datetime(self.state.refresh_at)
        seconds = max(0.0, (refresh_at - now).total_seconds())
        return max(1, math.ceil(seconds / 86400))

    def daily_limit(self, now: datetime) -> float:
        return self.state.day_start_remaining / self.remaining_days(now)

    def log_usage(self, amount: float, now: datetime) -> None:
        if amount <= 0:
            raise ValueError("消耗额度必须大于 0。")
        self.normalize(now)
        self.state.today_used += amount
        self.state.remaining_quota = max(0.0, self.state.remaining_quota - amount)

    def should_pause(self, now: datetime) -> bool:
        self.normalize(now)
        return self.state.today_used >= self.daily_limit(now)

    def sync_remote_usage(
        self, remaining_quota: float, refresh_at: datetime | None, now: datetime
    ) -> None:
        if not 0 <= remaining_quota <= 100:
            raise ValueError("网页返回的剩余额度不在 0 到 100 之间。")
        old_remaining = self.state.remaining_quota
        old_refresh = self.state.refresh_at
        if refresh_at:
            self.state.refresh_at = format_datetime(refresh_at)
        if not self.state.refresh_at:
            raise ValueError("网页未识别出刷新时间，请先手动填写一次。")
        self.normalize(now)
        refresh_changed = old_refresh and self.state.refresh_at != old_refresh
        if remaining_quota > old_remaining and refresh_changed:
            self.state.day_key = now.date().isoformat()
            self.state.day_start_remaining = remaining_quota
            self.state.today_used = 0.0
        elif remaining_quota < old_remaining:
            self.state.today_used += old_remaining - remaining_quota
        self.state.remaining_quota = remaining_quota


class QuotaGuardApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("项目额度守卫")
        self.root.geometry("720x730")
        self.state = QuotaState.load()
        self.controller = QuotaController(self.state)
        self.process: subprocess.Popen[str] | None = None
        self.status_text = tk.StringVar(value="未启动项目")

        self.command_var = tk.StringVar(value=self.state.command)
        self.project_dir_var = tk.StringVar(value=self.state.project_dir)
        self.remaining_var = tk.StringVar(value=f"{self.state.remaining_quota:.2f}")
        self.refresh_var = tk.StringVar(value=self.state.refresh_at)
        self.usage_var = tk.StringVar()
        self.auto_sync_var = tk.BooleanVar(value=self.state.auto_sync)
        self.sync_status_text = tk.StringVar(value="Codex 网页额度尚未同步")
        self.next_auto_sync = 0.0
        self.sync_in_progress = False

        self._build_ui()
        self._tick()

    def _build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=16)
        main.pack(fill=tk.BOTH, expand=True)
        main.columnconfigure(1, weight=1)

        ttk.Label(main, text="项目启动命令").grid(row=0, column=0, sticky=tk.W, pady=5)
        ttk.Entry(main, textvariable=self.command_var).grid(
            row=0, column=1, columnspan=2, sticky=tk.EW, pady=5
        )

        ttk.Label(main, text="项目目录").grid(row=1, column=0, sticky=tk.W, pady=5)
        ttk.Entry(main, textvariable=self.project_dir_var).grid(
            row=1, column=1, sticky=tk.EW, pady=5
        )
        ttk.Button(main, text="选择", command=self._choose_dir).grid(
            row=1, column=2, padx=(8, 0), pady=5
        )

        ttk.Label(main, text="当前剩余额度 (%)").grid(row=2, column=0, sticky=tk.W, pady=5)
        ttk.Entry(main, textvariable=self.remaining_var).grid(
            row=2, column=1, columnspan=2, sticky=tk.EW, pady=5
        )

        ttk.Label(main, text="下次刷新时间").grid(row=3, column=0, sticky=tk.W, pady=5)
        ttk.Entry(main, textvariable=self.refresh_var).grid(
            row=3, column=1, columnspan=2, sticky=tk.EW, pady=5
        )
        ttk.Label(main, text="格式：2026-06-08 22:00").grid(
            row=4, column=1, columnspan=2, sticky=tk.W
        )

        ttk.Checkbutton(
            main, text="每 5 分钟自动同步 Codex 网页额度", variable=self.auto_sync_var
        ).grid(row=5, column=0, columnspan=3, sticky=tk.W, pady=(12, 3))
        ttk.Button(main, text="登录 Codex 网页", command=self._login_codex).grid(
            row=6, column=0, sticky=tk.W, pady=5
        )
        ttk.Button(main, text="立即同步网页额度", command=self._sync_codex).grid(
            row=6, column=1, sticky=tk.W, pady=5
        )
        ttk.Label(main, textvariable=self.sync_status_text).grid(
            row=7, column=0, columnspan=3, sticky=tk.W, pady=(2, 8)
        )

        ttk.Button(main, text="保存设置", command=self._save_settings).grid(
            row=8, column=0, sticky=tk.W, pady=(8, 10)
        )
        ttk.Button(main, text="启动项目", command=self._start_project).grid(
            row=8, column=1, sticky=tk.W, pady=(8, 10)
        )
        ttk.Button(main, text="停止项目", command=self._stop_project).grid(
            row=8, column=2, sticky=tk.E, pady=(8, 10)
        )

        ttk.Separator(main).grid(row=9, column=0, columnspan=3, sticky=tk.EW, pady=8)

        ttk.Label(main, text="手动登记本次消耗额度 (%)").grid(row=10, column=0, sticky=tk.W, pady=5)
        ttk.Entry(main, textvariable=self.usage_var).grid(row=10, column=1, sticky=tk.EW, pady=5)
        ttk.Button(main, text="登记", command=self._log_usage).grid(
            row=10, column=2, padx=(8, 0), pady=5
        )

        self.summary = ttk.Label(main, text="", justify=tk.LEFT)
        self.summary.grid(row=11, column=0, columnspan=3, sticky=tk.W, pady=(18, 8))

        ttk.Label(main, textvariable=self.status_text).grid(
            row=12, column=0, columnspan=3, sticky=tk.W, pady=8
        )

        ttk.Label(
            main,
            text=(
                "说明：网页登录状态保存在本机独立浏览器目录中，不保存密码。"
                "自动同步失败时仍可手动登记。达到今日建议额度后，正在运行的项目会被自动结束。"
            ),
            wraplength=670,
        ).grid(row=13, column=0, columnspan=3, sticky=tk.W, pady=(14, 0))

    def _choose_dir(self) -> None:
        directory = filedialog.askdirectory()
        if directory:
            self.project_dir_var.set(directory)

    def _read_form(self) -> None:
        command = self.command_var.get().strip()
        project_dir = self.project_dir_var.get().strip()
        remaining = float(self.remaining_var.get().strip())
        refresh_at = parse_datetime(self.refresh_var.get())
        if not command:
            raise ValueError("请填写项目启动命令。")
        if not project_dir or not Path(project_dir).is_dir():
            raise ValueError("请选择有效的项目目录。")
        if not 0 <= remaining <= 100:
            raise ValueError("剩余额度必须在 0 到 100 之间。")
        if refresh_at <= datetime.now():
            raise ValueError("下次刷新时间必须晚于当前时间。")

        remaining_changed = abs(remaining - self.state.remaining_quota) > 1e-9
        refresh_changed = format_datetime(refresh_at) != self.state.refresh_at
        self.state.command = command
        self.state.project_dir = project_dir
        self.state.refresh_at = format_datetime(refresh_at)
        self.state.remaining_quota = remaining
        self.state.auto_sync = self.auto_sync_var.get()
        if remaining_changed or refresh_changed or not self.state.day_key:
            self.state.day_key = datetime.now().date().isoformat()
            self.state.day_start_remaining = remaining
            self.state.today_used = 0.0

    def _save_settings(self) -> bool:
        try:
            self._read_form()
            self.controller.normalize(datetime.now())
            self.state.save()
            self._update_summary()
            self.status_text.set("设置已保存")
            return True
        except (OSError, ValueError) as exc:
            messagebox.showerror("无法保存", str(exc))
            return False

    def _login_codex(self) -> None:
        if not (APP_DIR / "node_modules" / "playwright-core").exists():
            messagebox.showerror("缺少组件", "请先双击“安装浏览器读取组件.bat”。")
            return
        try:
            subprocess.Popen(
                [str(NODE_EXECUTABLE), str(READER_SCRIPT), "login"],
                cwd=APP_DIR,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            )
            self.sync_status_text.set("登录浏览器已打开。登录完成后请关闭该浏览器窗口。")
        except OSError as exc:
            messagebox.showerror("无法打开浏览器", str(exc))

    def _sync_codex(self) -> None:
        if self.sync_in_progress:
            return
        if not (APP_DIR / "node_modules" / "playwright-core").exists():
            self.sync_status_text.set("尚未安装浏览器读取组件")
            self.next_auto_sync = time.monotonic() + AUTO_SYNC_INTERVAL_SECONDS
            return
        self.sync_in_progress = True
        self.sync_status_text.set("正在同步 Codex 网页额度...")
        threading.Thread(target=self._fetch_codex_usage, daemon=True).start()

    def _fetch_codex_usage(self) -> None:
        try:
            result = subprocess.run(
                [str(NODE_EXECUTABLE), str(READER_SCRIPT), "fetch"],
                cwd=APP_DIR,
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or "网页额度同步失败。")
            data = json.loads(result.stdout.strip())
            self.root.after(0, self._apply_codex_usage, data)
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            self.root.after(0, self._finish_sync_error, str(exc))

    def _apply_codex_usage(self, data: dict[str, object]) -> None:
        try:
            refresh_at = None
            if data.get("refreshAt"):
                refresh_at = datetime.fromisoformat(str(data["refreshAt"]).replace("Z", "+00:00"))
                refresh_at = refresh_at.astimezone().replace(tzinfo=None)
            now = datetime.now()
            self.controller.sync_remote_usage(float(data["remainingQuota"]), refresh_at, now)
            self.state.last_sync_at = format_datetime(now)
            self.state.auto_sync = self.auto_sync_var.get()
            self.state.save()
            self.remaining_var.set(f"{self.state.remaining_quota:.2f}")
            self.refresh_var.set(self.state.refresh_at)
            self._update_summary()
            self.sync_status_text.set(f"Codex 网页额度已同步：{self.state.last_sync_at}")
            if self.controller.should_pause(now):
                self._stop_project(automatic=True)
        except (OSError, TypeError, ValueError) as exc:
            self.sync_status_text.set(f"同步结果无法应用：{exc}")
        finally:
            self.sync_in_progress = False
            self.next_auto_sync = time.monotonic() + AUTO_SYNC_INTERVAL_SECONDS

    def _finish_sync_error(self, error: str) -> None:
        self.sync_in_progress = False
        self.next_auto_sync = time.monotonic() + AUTO_SYNC_INTERVAL_SECONDS
        self.sync_status_text.set(f"同步失败：{error}")

    def _start_project(self) -> None:
        if self.process and self.process.poll() is None:
            messagebox.showinfo("项目已运行", "当前项目已经在运行。")
            return
        if not self._save_settings():
            return
        if self.controller.should_pause(datetime.now()):
            messagebox.showwarning("今日额度已用完", "今日额度已达到上限，无法启动项目。")
            return
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        try:
            self.process = subprocess.Popen(
                self.state.command,
                cwd=self.state.project_dir,
                shell=True,
                creationflags=creationflags,
                start_new_session=os.name != "nt",
                text=True,
            )
            self.status_text.set(f"项目运行中，PID: {self.process.pid}")
        except OSError as exc:
            messagebox.showerror("启动失败", str(exc))

    def _stop_project(self, automatic: bool = False) -> None:
        if not self.process or self.process.poll() is not None:
            self.process = None
            self.status_text.set("项目未运行")
            return
        pid = self.process.pid
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    check=False,
                    capture_output=True,
                    text=True,
                )
            else:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            self.process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            self.process.kill()
        finally:
            self.process = None
        if automatic:
            self.status_text.set("今日额度已达到上限，项目已自动停止")
            messagebox.showwarning("项目已暂停", "今日额度已达到上限，项目已自动停止。")
        else:
            self.status_text.set("项目已停止")

    def _log_usage(self) -> None:
        try:
            amount = float(self.usage_var.get().strip())
            self.controller.log_usage(amount, datetime.now())
            self.state.save()
            self.remaining_var.set(f"{self.state.remaining_quota:.2f}")
            self.usage_var.set("")
            self._update_summary()
            if self.controller.should_pause(datetime.now()):
                self._stop_project(automatic=True)
        except (OSError, ValueError) as exc:
            messagebox.showerror("无法登记", str(exc))

    def _update_summary(self) -> None:
        if not self.state.refresh_at:
            self.summary.config(text="请先填写设置。")
            return
        now = datetime.now()
        try:
            self.controller.normalize(now)
            limit = self.controller.daily_limit(now)
            days = self.controller.remaining_days(now)
            available = max(0.0, limit - self.state.today_used)
            self.summary.config(
                text=(
                    f"当前剩余额度：{self.state.remaining_quota:.2f}%\n"
                    f"距离刷新剩余天数：{days}\n"
                    f"今日建议额度：{limit:.2f}%\n"
                    f"今日已使用：{self.state.today_used:.2f}%\n"
                    f"今日还可使用：{available:.2f}%"
                )
            )
        except ValueError:
            self.summary.config(text="请填写有效的刷新时间。")

    def _tick(self) -> None:
        if self.process and self.process.poll() is not None:
            self.process = None
            self.status_text.set("项目已退出")
        if self.state.refresh_at:
            try:
                self.controller.normalize(datetime.now())
                self.state.save()
                self.remaining_var.set(f"{self.state.remaining_quota:.2f}")
                self._update_summary()
                if self.controller.should_pause(datetime.now()):
                    self._stop_project(automatic=True)
            except (OSError, ValueError):
                pass
        if self.auto_sync_var.get() and time.monotonic() >= self.next_auto_sync:
            self._sync_codex()
        self.root.after(CHECK_INTERVAL_MS, self._tick)


def main() -> None:
    root = tk.Tk()
    QuotaGuardApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
