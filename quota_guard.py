from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


def resolve_state_file(app_dir: Path, environ: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if environ is None else environ
    local_app_data = environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "CodexQuotaGuard" / "quota_guard_state.json"
    if os.name == "nt":
        return Path.home() / "AppData" / "Local" / "CodexQuotaGuard" / "quota_guard_state.json"
    return Path.home() / ".config" / "CodexQuotaGuard" / "quota_guard_state.json"


LEGACY_STATE_FILE = APP_DIR / "quota_guard_state.json"
STATE_FILE = resolve_state_file(APP_DIR)
CHECK_INTERVAL_MS = 1000
READER_SCRIPT = APP_DIR / "codex_usage_reader.js"
NODE_EXECUTABLE = APP_DIR / "node.exe" if (APP_DIR / "node.exe").exists() else "node"
SYNC_INTERVAL_OPTIONS = (5, 15, 30, 60)
PAUSE_POLICIES = ("仅提醒", "提醒后停止", "立即停止")
SUBPROCESS_CREATION_FLAGS = (
    subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    if os.name == "nt"
    else 0
)


def parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.strip())


def format_datetime(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M")


def format_process_error(stderr: str | None, stdout: str | None) -> str:
    return (stderr or stdout or "网页额度同步失败。").strip() or "网页额度同步失败。"


def format_sync_status_error(error: str, limit: int = 240) -> str:
    compact = " ".join(error.split())
    if len(compact) > limit:
        compact = compact[: limit - 1].rstrip() + "…"
    return f"同步失败：{compact}"


def reader_env(use_system_chrome_profile: bool, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if environ is None else environ)
    if use_system_chrome_profile:
        env["CODEX_QUOTA_GUARD_PROFILE_MODE"] = "system-chrome"
    else:
        env.pop("CODEX_QUOTA_GUARD_PROFILE_MODE", None)
    return env


def login_status_text(use_system_chrome_profile: bool) -> str:
    if use_system_chrome_profile:
        return "已用系统浏览器打开 Usage 页面。登录完成并看到额度后，请关闭普通 Chrome 再同步。"
    return "登录浏览器已打开。登录完成后请关闭该浏览器窗口。"


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
    monitor_only: bool = True
    sync_interval_minutes: int = 5
    pause_policy: str = "仅提醒"
    use_system_chrome_profile: bool = False

    @classmethod
    def load(cls) -> "QuotaState":
        return cls.load_from_files(STATE_FILE, LEGACY_STATE_FILE)

    @classmethod
    def load_from_files(cls, state_file: Path, legacy_state_file: Path) -> "QuotaState":
        source_file = state_file if state_file.exists() else legacy_state_file
        if not source_file.exists():
            return cls()
        try:
            state = cls(**json.loads(source_file.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return cls()
        if source_file == legacy_state_file and not state_file.exists():
            try:
                state_file.parent.mkdir(parents=True, exist_ok=True)
                legacy_state_file.replace(state_file)
            except OSError:
                pass
        return state

    def save(self) -> None:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
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
        if not self.state.last_sync_at:
            self.state.day_key = now.date().isoformat()
            self.state.day_start_remaining = remaining_quota
            self.state.today_used = 0.0
        elif remaining_quota > old_remaining and refresh_changed:
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
        self.root.geometry("760x830")
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
        self.use_system_chrome_profile_var = tk.BooleanVar(value=self.state.use_system_chrome_profile)
        self.monitor_only_var = tk.BooleanVar(value=self.state.monitor_only)
        self.sync_interval_var = tk.StringVar(value=str(self.state.sync_interval_minutes))
        self.pause_policy_var = tk.StringVar(value=self.state.pause_policy)
        self.sync_status_text = tk.StringVar(value="Codex 网页额度尚未同步")
        self.mode_text = tk.StringVar()
        self.next_auto_sync = 0.0
        self.sync_in_progress = False
        self.warned_levels: set[int] = set()

        self._build_ui()
        self._update_mode()
        self._tick()

    def _build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=16)
        main.pack(fill=tk.BOTH, expand=True)
        main.columnconfigure(1, weight=1)

        ttk.Checkbutton(
            main, text="仅监控额度，不启动或停止本地项目", variable=self.monitor_only_var,
            command=self._update_mode,
        ).grid(row=0, column=0, columnspan=3, sticky=tk.W, pady=(0, 8))
        ttk.Label(main, textvariable=self.mode_text).grid(
            row=1, column=0, columnspan=3, sticky=tk.W, pady=(0, 6)
        )

        ttk.Label(main, text="项目启动命令").grid(row=2, column=0, sticky=tk.W, pady=5)
        self.command_entry = ttk.Entry(main, textvariable=self.command_var)
        self.command_entry.grid(
            row=2, column=1, columnspan=2, sticky=tk.EW, pady=5
        )

        ttk.Label(main, text="项目目录").grid(row=3, column=0, sticky=tk.W, pady=5)
        self.project_dir_entry = ttk.Entry(main, textvariable=self.project_dir_var)
        self.project_dir_entry.grid(
            row=3, column=1, sticky=tk.EW, pady=5
        )
        self.choose_dir_button = ttk.Button(main, text="选择", command=self._choose_dir)
        self.choose_dir_button.grid(
            row=3, column=2, padx=(8, 0), pady=5
        )

        ttk.Label(main, text="当前剩余额度 (%)").grid(row=4, column=0, sticky=tk.W, pady=5)
        ttk.Entry(main, textvariable=self.remaining_var).grid(
            row=4, column=1, columnspan=2, sticky=tk.EW, pady=5
        )

        ttk.Label(main, text="下次刷新时间").grid(row=5, column=0, sticky=tk.W, pady=5)
        ttk.Entry(main, textvariable=self.refresh_var).grid(
            row=5, column=1, columnspan=2, sticky=tk.EW, pady=5
        )
        ttk.Label(main, text="格式示例：年-月-日 时:分，例如 2026-06-08 22:00").grid(
            row=6, column=1, columnspan=2, sticky=tk.W
        )

        ttk.Checkbutton(
            main, text="自动同步 Codex 网页额度", variable=self.auto_sync_var
        ).grid(row=7, column=0, sticky=tk.W, pady=(12, 3))
        ttk.Label(main, text="同步间隔（分钟）").grid(row=7, column=1, sticky=tk.E, pady=(12, 3))
        ttk.Combobox(
            main, textvariable=self.sync_interval_var,
            values=tuple(str(value) for value in SYNC_INTERVAL_OPTIONS),
            state="readonly", width=5,
        ).grid(row=7, column=2, sticky=tk.W, padx=(8, 0), pady=(12, 3))
        ttk.Button(main, text="登录 Codex 网页", command=self._login_codex).grid(
            row=8, column=0, sticky=tk.W, pady=5
        )
        ttk.Button(main, text="立即同步网页额度", command=self._sync_codex).grid(
            row=8, column=1, sticky=tk.W, pady=5
        )
        ttk.Checkbutton(
            main,
            text="使用系统 Chrome 登录状态（同步前请关闭普通 Chrome）",
            variable=self.use_system_chrome_profile_var,
        ).grid(row=9, column=0, columnspan=3, sticky=tk.W, pady=(2, 4))
        ttk.Label(main, textvariable=self.sync_status_text).grid(
            row=10, column=0, columnspan=3, sticky=tk.W, pady=(2, 8)
        )

        ttk.Button(main, text="保存设置", command=self._save_settings).grid(
            row=11, column=0, sticky=tk.W, pady=(8, 10)
        )
        self.start_button = ttk.Button(main, text="启动项目", command=self._start_project)
        self.start_button.grid(
            row=11, column=1, sticky=tk.W, pady=(8, 10)
        )
        self.stop_button = ttk.Button(main, text="停止项目", command=self._stop_project)
        self.stop_button.grid(
            row=11, column=2, sticky=tk.E, pady=(8, 10)
        )

        ttk.Label(main, text="达到今日上限时").grid(row=12, column=0, sticky=tk.W, pady=5)
        self.pause_policy_combo = ttk.Combobox(
            main, textvariable=self.pause_policy_var, values=PAUSE_POLICIES,
            state="readonly", width=12,
        )
        self.pause_policy_combo.grid(row=12, column=1, sticky=tk.W, pady=5)

        ttk.Separator(main).grid(row=13, column=0, columnspan=3, sticky=tk.EW, pady=8)

        ttk.Label(main, text="手动登记本次消耗额度 (%)").grid(row=14, column=0, sticky=tk.W, pady=5)
        ttk.Entry(main, textvariable=self.usage_var).grid(row=14, column=1, sticky=tk.EW, pady=5)
        ttk.Button(main, text="登记", command=self._log_usage).grid(
            row=14, column=2, padx=(8, 0), pady=5
        )

        self.summary = ttk.Label(main, text="", justify=tk.LEFT)
        self.summary.grid(row=15, column=0, columnspan=3, sticky=tk.W, pady=(18, 8))

        ttk.Label(main, textvariable=self.status_text).grid(
            row=16, column=0, columnspan=3, sticky=tk.W, pady=8
        )

        ttk.Label(
            main,
            text=(
                "说明：默认网页登录状态保存在本机独立浏览器目录中，不保存密码。"
                "如使用系统 Chrome 登录状态，工具会读取普通 Chrome 的登录目录；同步前需关闭普通 Chrome。"
                "自动同步失败时仍可手动登记。"
            ),
            wraplength=670,
        ).grid(row=17, column=0, columnspan=3, sticky=tk.W, pady=(14, 0))

    def _update_mode(self) -> None:
        monitor_only = self.monitor_only_var.get()
        self.mode_text.set(f"当前模式：{'仅监控额度' if monitor_only else '项目守卫'}")
        state = tk.DISABLED if monitor_only else tk.NORMAL
        for widget in (
            self.command_entry, self.project_dir_entry, self.choose_dir_button,
            self.start_button, self.stop_button,
        ):
            widget.configure(state=state)
        self.pause_policy_combo.configure(state=tk.DISABLED if monitor_only else "readonly")
        self.state.monitor_only = monitor_only
        if monitor_only:
            self.status_text.set("仅监控额度，不管理本地项目")

    def _choose_dir(self) -> None:
        directory = filedialog.askdirectory()
        if directory:
            self.project_dir_var.set(directory)

    def _read_form(self) -> None:
        monitor_only = self.monitor_only_var.get()
        command = self.command_var.get().strip()
        project_dir = self.project_dir_var.get().strip()
        remaining = float(self.remaining_var.get().strip())
        refresh_at = parse_datetime(self.refresh_var.get())
        if not monitor_only and not command:
            raise ValueError("请填写项目启动命令。")
        if not monitor_only and (not project_dir or not Path(project_dir).is_dir()):
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
        self.state.use_system_chrome_profile = self.use_system_chrome_profile_var.get()
        self.state.monitor_only = monitor_only
        self.state.sync_interval_minutes = int(self.sync_interval_var.get())
        self.state.pause_policy = self.pause_policy_var.get()
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

    def _reader_env(self) -> dict[str, str]:
        return reader_env(self.use_system_chrome_profile_var.get())

    def _login_codex(self) -> None:
        if self.use_system_chrome_profile_var.get():
            webbrowser.open("https://chatgpt.com/codex/settings/usage")
            self.sync_status_text.set(login_status_text(True))
            return
        if not (APP_DIR / "node_modules" / "playwright-core").exists():
            messagebox.showerror("缺少组件", "请先双击 install_browser_reader.bat。")
            return
        try:
            subprocess.Popen(
                [str(NODE_EXECUTABLE), str(READER_SCRIPT), "login"],
                cwd=APP_DIR,
                env=self._reader_env(),
                creationflags=SUBPROCESS_CREATION_FLAGS,
            )
            self.sync_status_text.set(login_status_text(False))
        except OSError as exc:
            messagebox.showerror("无法打开浏览器", str(exc))

    def _sync_codex(self) -> None:
        if self.sync_in_progress:
            return
        if not (APP_DIR / "node_modules" / "playwright-core").exists():
            self.sync_status_text.set("尚未安装浏览器读取组件")
            self.next_auto_sync = time.monotonic() + self._sync_interval_seconds()
            return
        self.sync_in_progress = True
        self.sync_status_text.set("正在同步 Codex 网页额度...")
        threading.Thread(target=self._fetch_codex_usage, daemon=True).start()

    def _fetch_codex_usage(self) -> None:
        try:
            result = subprocess.run(
                [str(NODE_EXECUTABLE), str(READER_SCRIPT), "fetch"],
                cwd=APP_DIR,
                env=self._reader_env(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=45,
                check=False,
                creationflags=SUBPROCESS_CREATION_FLAGS,
            )
            if result.returncode != 0:
                raise RuntimeError(format_process_error(result.stderr, result.stdout))
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
            self.state.use_system_chrome_profile = self.use_system_chrome_profile_var.get()
            self.state.save()
            self.remaining_var.set(f"{self.state.remaining_quota:.2f}")
            self.refresh_var.set(self.state.refresh_at)
            self._update_summary()
            self.sync_status_text.set(f"Codex 网页额度已同步：{self.state.last_sync_at}")
            self._handle_usage_thresholds(now)
        except (OSError, TypeError, ValueError) as exc:
            self.sync_status_text.set(f"同步结果无法应用：{exc}")
        finally:
            self.sync_in_progress = False
            self.next_auto_sync = time.monotonic() + self._sync_interval_seconds()

    def _finish_sync_error(self, error: str) -> None:
        self.sync_in_progress = False
        self.next_auto_sync = time.monotonic() + self._sync_interval_seconds()
        self.sync_status_text.set(format_sync_status_error(error))

    def _start_project(self) -> None:
        if self.monitor_only_var.get():
            messagebox.showinfo("仅监控模式", "当前为仅监控额度模式，不会启动本地项目。")
            return
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
            self._handle_usage_thresholds(datetime.now())
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
                    f"今日还可使用：{available:.2f}%\n"
                    f"最后同步时间：{self.state.last_sync_at or '尚未同步'}\n"
                    f"下次自动同步：{self._next_sync_text()}"
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
                self._handle_usage_thresholds(datetime.now())
            except (OSError, ValueError):
                pass
        if self.auto_sync_var.get() and time.monotonic() >= self.next_auto_sync:
            self._sync_codex()
        self.root.after(CHECK_INTERVAL_MS, self._tick)

    def _sync_interval_seconds(self) -> int:
        return int(self.sync_interval_var.get()) * 60

    def _next_sync_text(self) -> str:
        if not self.auto_sync_var.get():
            return "未开启"
        seconds = max(0, math.ceil(self.next_auto_sync - time.monotonic()))
        return f"约 {seconds // 60:02d}:{seconds % 60:02d} 后"

    def _handle_usage_thresholds(self, now: datetime) -> None:
        limit = self.controller.daily_limit(now)
        available = max(0.0, limit - self.state.today_used)
        ratio = 0.0 if limit <= 0 else available / limit
        for level in (20, 10):
            if ratio <= level / 100 and level not in self.warned_levels:
                self.warned_levels.add(level)
                messagebox.showwarning("额度预警", f"今日建议额度仅剩 {available:.2f}% 可用。")
        if not self.controller.should_pause(now):
            return
        policy = self.pause_policy_var.get()
        if policy == "仅提醒" or self.monitor_only_var.get():
            if 0 not in self.warned_levels:
                self.warned_levels.add(0)
                messagebox.showwarning("今日额度已用完", "今日建议额度已达到上限。")
            return
        if policy == "提醒后停止":
            messagebox.showwarning("项目即将停止", "今日建议额度已达到上限，项目将被停止。")
        self._stop_project(automatic=True)


def main() -> None:
    root = tk.Tk()
    QuotaGuardApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
