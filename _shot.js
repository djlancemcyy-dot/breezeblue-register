const pw = require("C:\\Users\\lee\\AppData\\Roaming\\npm\\node_modules\\@playwright\\cli\\node_modules\\playwright-core");
(async () => {
  const b = await pw.chromium.launch({ channel: "chrome", headless: true });
  const p = await b.newPage({ viewport: { width: 1280, height: 900 } });
  const errs = [];
  p.on("pageerror", e => errs.push(String(e.message).slice(0, 120)));
  await p.goto("http://127.0.0.1:8799/", { waitUntil: "networkidle", timeout: 30000 });
  await p.waitForTimeout(3000);
  await p.screenshot({ path: "data/panel-accounts.png", fullPage: false });
  await p.click('button[data-tab="logs"]');
  await p.waitForTimeout(1500);
  await p.screenshot({ path: "data/panel-logs.png" });
  const counts = await p.evaluate(() => ({ rows: document.querySelectorAll("#accBody tr").length,
                                           status: document.querySelector("#status").textContent,
                                           prog: document.querySelector("#progressText").textContent }));
  console.log(JSON.stringify({ counts, errs }));
  await b.close();
})().catch(e => { console.log("FATAL", e.message); process.exit(1); });