const assert = require("assert");
const {
  describeLaunchError,
  describePageBlocker,
  parseUsageText,
  resolveProfileDir,
} = require("./codex_usage_reader.js");

const englishText = `
Account
Weekly usage limit
73% remaining
Resets at 2026-06-08 12:00
`;

const chineseText = `
设置
每周使用额度
剩余 65%
重置于 2026年6月8日 12:00
`;

const englishUsage = parseUsageText(englishText);
assert.strictEqual(englishUsage.remainingQuota, 73);
assert.strictEqual(englishUsage.refreshAt, new Date(2026, 5, 8, 12, 0).toISOString());

const chineseUsage = parseUsageText(chineseText);
assert.strictEqual(chineseUsage.remainingQuota, 65);
assert.strictEqual(chineseUsage.refreshAt, new Date(2026, 5, 8, 12, 0).toISOString());

assert.strictEqual(
  describePageBlocker("请稍候…", "", "<title>请稍候…</title><div>请验证您是真人</div>"),
  "页面正在等待 Cloudflare 真人验证。请点击“登录 Codex 网页”，在弹出的浏览器中完成验证并确认能看到 Usage 页面，然后再同步。"
);

assert.strictEqual(
  resolveProfileDir(
    { CODEX_QUOTA_GUARD_PROFILE_MODE: "system-chrome", LOCALAPPDATA: "C:\\Users\\Admin\\AppData\\Local" },
    "win32"
  ),
  "C:\\Users\\Admin\\AppData\\Local\\Google\\Chrome\\User Data"
);

assert.strictEqual(
  describeLaunchError(new Error("Failed to create a ProcessSingleton for your profile directory")),
  "系统 Chrome 登录目录正在被 Chrome 使用。请先关闭所有普通 Chrome 窗口，再点击“立即同步网页额度”。"
);
