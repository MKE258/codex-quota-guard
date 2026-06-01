const path = require("path");
const os = require("os");
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
    /weekly usage limit|weekly limit|每周(?:使用)?(?:额度|限制)|周(?:使用)?(?:额度|限制)/i.test(line)
  );
  if (index < 0) {
    throw new Error("未找到每周额度。请确认已登录，并已打开 Codex Usage 页面。");
  }
  return lines.slice(index, index + 8).join(" ");
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
  const match = block.match(/(?:resets?|reset(?:s)? at|重置(?:于|时间)?)[：:\s]*([^|]+)$/i);
  if (!match) {
    return null;
  }
  const value = match[1].trim();
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed.toISOString();
}

async function login() {
  const context = await launch(false);
  const pages = context.pages();
  const page = pages[0] || await context.newPage();
  await page.goto(USAGE_URL, { waitUntil: "domcontentloaded" });
  console.log("请在浏览器中登录并确认能看到 Codex Usage 页面。完成后关闭浏览器窗口。");
  await new Promise((resolve) => context.on("close", resolve));
}

async function fetchUsage() {
  const context = await launch(true);
  try {
    const pages = context.pages();
    const page = pages[0] || await context.newPage();
    await page.goto(USAGE_URL, { waitUntil: "domcontentloaded", timeout: 30000 });
    await page.waitForTimeout(2500);
    const text = await page.locator("body").innerText();
    if (/log in|sign in|登录|登入/i.test(text) && !/weekly usage limit|每周(?:使用)?(?:额度|限制)/i.test(text)) {
      throw new Error("登录状态已失效，请点击“登录 Codex 网页”重新登录。");
    }
    const block = findWeeklyBlock(text);
    console.log(JSON.stringify({
      remainingQuota: extractRemaining(block),
      refreshAt: extractResetAt(block),
      checkedAt: new Date().toISOString(),
    }));
  } finally {
    await context.close();
  }
}

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
