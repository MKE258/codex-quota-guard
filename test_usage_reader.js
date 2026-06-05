const assert = require("assert");
const { parseUsageText } = require("./codex_usage_reader.js");

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
