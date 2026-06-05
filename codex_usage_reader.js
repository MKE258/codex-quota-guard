const path = require("path");
const os = require("os");
const fs = require("fs");
const { spawn } = require("child_process");
const { chromium } = require("playwright-core");

const USAGE_URL = "https://chatgpt.com/codex/settings/usage";
const PROFILE_DIR = path.join(
  process.env.LOCALAPPDATA || path.join(os.homedir(), "AppData", "Local"),
  "CodexQuotaGuard",
  "browser-profile"
);

function browserOptions(headless) {
  return {
    channel: "chrome",
    headless,
    viewport: { width: 1280, height: 900 },
    args: headless ? [] : ["--window-position=-32000,-32000", "--window-size=1280,900"],
  };
}

async function launch(headless) {
  try {
    return await chromium.launchPersistentContext(PROFILE_DIR, browserOptions(headless));
  } catch (error) {
    return chromium.launchPersistentContext(PROFILE_DIR, {
      ...browserOptions(headless),
      channel: "msedge",
    });
  }
}

function findWeeklyBlock(text) {
  const lines = text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const index = lines.findIndex((line) =>
    /weekly usage limit|weekly limit|每周(?:使用)?(?:额度|限制|限额)|周(?:使用)?(?:额度|限制|限额)/i.test(line)
  );
  if (index < 0) {
    throw new Error("未找到每周额度。请确认已登录，并已打开 Codex Usage 页面。");
  }
  return lines.slice(index, index + 12).join(" ");
}

function extractRemaining(block) {
  const remaining = block.match(/(\d+(?:\.\d+)?)\s*%\s*(?:remaining|left|剩余)/i);
  if (remaining) {
    return Number(remaining[1]);
  }
  const anyPercent = block.match(/(\d+(?:\.\d+)?)\s*%/);
  if (anyPercent) {
    return Number(anyPercent[1]);
  }
  throw new Error("找到了每周额度区域，但无法识别剩余百分比。");
}

function extractResetAt(block) {
  const match = block.match(/(?:resets?|reset(?:s)? at|重置(?:于|时间)?)[：:\s]*((?:\d{4}年\d{1,2}月\d{1,2}日\s+\d{1,2}:\d{2})|(?:\d{4}-\d{1,2}-\d{1,2}(?:T|\s)\d{1,2}:\d{2}(?::\d{2})?))/i);
  if (!match) {
    return null;
  }
  const value = match[1].trim().replace(
    /(\d{4})年(\d{1,2})月(\d{1,2})日\s+(\d{1,2}):(\d{2})/,
    (_, year, month, day, hour, minute) =>
      `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}T${String(hour).padStart(2, "0")}:${minute}:00`
  );
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed.toISOString();
}

function parseUsageText(text) {
  const block = findWeeklyBlock(text);
  return {
    remainingQuota: extractRemaining(block),
    refreshAt: extractResetAt(block),
  };
}

function describePageBlocker(title, text, html) {
  const haystack = `${title}\n${text}\n${html}`;
  if (/请稍候|just a moment|verify you are human|验证您是真人|cloudflare/i.test(haystack)) {
    return "页面正在等待 Cloudflare 真人验证。请点击“登录 Codex 网页”，在弹出的浏览器中完成验证并确认能看到 Usage 页面，然后再同步。";
  }
  if (/log in|sign in|登录|登入/i.test(text) && !/weekly usage limit|每周(?:使用)?(?:额度|限制|限额)/i.test(text)) {
    return "登录状态已失效，请点击“登录 Codex 网页”重新登录。";
  }
  return null;
}

async function login() {
  const candidates = [
    path.join(process.env.PROGRAMFILES || "", "Google", "Chrome", "Application", "chrome.exe"),
    path.join(process.env["PROGRAMFILES(X86)"] || "", "Microsoft", "Edge", "Application", "msedge.exe"),
    path.join(process.env.PROGRAMFILES || "", "Microsoft", "Edge", "Application", "msedge.exe"),
  ];
  const executable = candidates.find((candidate) => fs.existsSync(candidate));
  if (!executable) {
    throw new Error("未找到 Chrome 或 Edge。");
  }
  const browser = spawn(executable, [
    `--user-data-dir=${PROFILE_DIR}`,
    "--no-first-run",
    USAGE_URL,
  ], { stdio: "ignore" });
  console.log("请在浏览器中登录并确认能看到 Codex Usage 页面。完成后关闭浏览器窗口。");
  await new Promise((resolve, reject) => {
    browser.on("error", reject);
    browser.on("close", resolve);
  });
}

async function fetchUsage() {
  const context = await launch(false);
  try {
    const pages = context.pages();
    const page = pages[0] || await context.newPage();
    await page.goto(USAGE_URL, { waitUntil: "domcontentloaded", timeout: 30000 });
    await page.waitForTimeout(6000);
    const title = await page.title();
    const text = await page.locator("body").innerText();
    const html = text ? "" : await page.content();
    const blocker = describePageBlocker(title, text, html);
    if (blocker) {
      throw new Error(blocker);
    }
    let usage;
    try {
      usage = parseUsageText(text);
    } catch (error) {
      throw new Error(`${error.message} 当前页面：${page.url()}，文本长度：${text.length}`);
    }
    console.log(JSON.stringify({
      remainingQuota: usage.remainingQuota,
      refreshAt: usage.refreshAt,
      checkedAt: new Date().toISOString(),
    }));
  } finally {
    await context.close();
  }
}

if (require.main === module) {
  const command = process.argv[2];
  if (command === "login") {
    login().catch((error) => {
      console.error(error.message);
      process.exit(1);
    });
  } else if (command === "fetch") {
    fetchUsage().catch((error) => {
      console.error(error.message);
      process.exit(1);
    });
  } else {
    console.error("用法: node codex_usage_reader.js login|fetch");
    process.exit(1);
  }
}

module.exports = {
  describePageBlocker,
  parseUsageText,
};
