"use strict";

const { spawnSync } = require("node:child_process");

// 自动发现全部 service_contract / test_* 测试模块
const result = spawnSync("python3", ["-m", "unittest", "discover", "-v", "-p", "*test*.py"], {
  stdio: "inherit",
});
if (result.error) {
  console.error(result.error.message);
  process.exit(1);
}
process.exit(result.status ?? 1);
