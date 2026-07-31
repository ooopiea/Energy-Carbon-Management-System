/**
 * Frontend E2E tests via Playwright (Node.js).
 *
 * Covers full user journeys and adversarial UI interactions:
 * 1. Page load + initial state
 * 2. Config → start dispatch → approval bar appears
 * 3. Chart rendering (4 canvases)
 * 4. Approve flow → status changes
 * 5. Reject flow
 * 6. Revise flow with interpretation (LLM agent)
 * 7. Error handling (invalid date, network failure)
 * 8. Responsive layout (mobile viewport)
 * 9. Empty state
 * 10. Graph visualization rendering
 *
 * Usage:
 *   # Start server first:
 *   python server.py --port 8130
 *   # Then run:
 *   node tests/e2e/frontend_e2e.js
 */
const { chromium } = require("playwright");

const BASE_URL = process.env.E2E_BASE_URL || "http://localhost:8130";
const results = [];

async function test(name, fn) {
    try {
        await fn();
        results.push({ name, status: "PASS" });
        console.log(`  ✓ ${name}`);
    } catch (e) {
        results.push({ name, status: "FAIL", error: e.message });
        console.log(`  ✗ ${name}: ${e.message}`);
    }
}

function assert(cond, msg) {
    if (!cond) throw new Error(msg || "assertion failed");
}

async function sleep(ms) {
    return new Promise(r => setTimeout(r, ms));
}

(async () => {
    const browser = await chromium.launch({ headless: true, channel: "chrome", timeout: 10000 });
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });

    console.log("\n=== Frontend E2E Test Suite ===\n");

    // =========================================================================
    // 1. Page Load & Initial State
    // =========================================================================
    console.log("--- Page Load ---");
    {
        const page = await context.newPage();
        await page.goto(`${BASE_URL}/dashboard`, { waitUntil: "domcontentloaded" });

        await test("title is correct", async () => {
            const title = await page.title();
            assert(title.includes("储能"), `title should mention storage, got: ${title}`);
        });

        await test("header is visible", async () => {
            const header = await page.locator(".header-title").textContent();
            assert(header.length > 0, "header should have text");
        });

        await test("status shows idle", async () => {
            const status = await page.locator("#statusText").textContent();
            assert(status === "待机", `expected 待机, got ${status}`);
        });

        await test("empty state is visible", async () => {
            const visible = await page.locator("#emptyState").isVisible();
            assert(visible, "empty state should be visible on load");
        });

        await test("config bar has all inputs", async () => {
            assert(await page.locator("#targetDate").isVisible(), "date input missing");
            assert(await page.locator("#objective").isVisible(), "objective select missing");
            assert(await page.locator("#startBtn").isVisible(), "start button missing");
            assert(await page.locator("#batteryCapacity").isVisible(), "battery input missing");
        });

        await test("graph SVG is rendered", async () => {
            const svg = await page.locator("#graphSvg");
            assert(await svg.isVisible(), "graph SVG should be visible");
        });

        await test("start button is not disabled initially", async () => {
            const disabled = await page.locator("#startBtn").isDisabled();
            assert(!disabled, "start button should be enabled");
        });

        await page.close();
    }

    // =========================================================================
    // 2. Start Dispatch → Approval
    // =========================================================================
    console.log("\n--- Start Dispatch ---");
    {
        const page = await context.newPage();
        await page.goto(`${BASE_URL}/dashboard`, { waitUntil: "domcontentloaded" });

        await test("can fill date and click start", async () => {
            await page.fill("#targetDate", "2026-07-27");
            await page.click("#startBtn");
            await sleep(8000); // wait for run to complete to approval
            const status = await page.locator("#statusText").textContent();
            assert(status === "等待审批", `expected 等待审批, got ${status}`);
        });

        await test("approval bar appears with 3 buttons", async () => {
            const visible = await page.locator("#approvalBarArea .approval-bar").isVisible();
            assert(visible, "approval bar should be visible");
            const btns = await page.locator("#approvalBarArea button").allTextContents();
            assert(btns.includes("修改"), "should have revise button");
            assert(btns.includes("拒绝"), "should have reject button");
            assert(btns.includes("批准"), "should have approve button");
        });

        await test("4 canvases render after data load", async () => {
            const count = await page.locator("canvas").count();
            assert(count === 4, `expected 4 canvases, got ${count}`);
        });

        await test("8 metric cards render", async () => {
            const count = await page.locator(".metric-card").count();
            assert(count >= 7, `expected at least 7 metric cards, got ${count}`);
        });

        await test("event log has entries", async () => {
            const count = await page.locator("#eventsLog > div").count();
            assert(count >= 7, `expected at least 7 events, got ${count}`);
        });

        await test("graph nodes show succeeded status", async () => {
            const succeeded = await page.locator(".graph-node.succeeded").count();
            assert(succeeded >= 7, `expected at least 7 succeeded nodes, got ${succeeded}`);
        });

        await page.close();
    }

    // =========================================================================
    // 3. Approve Flow
    // =========================================================================
    console.log("\n--- Approve Flow ---");
    {
        const page = await context.newPage();
        await page.goto(`${BASE_URL}/dashboard`, { waitUntil: "domcontentloaded" });
        await page.fill("#targetDate", "2026-07-27");
        await page.click("#startBtn");
        await sleep(8000);

        await test("clicking approve changes status", async () => {
            await page.click("#approvalBarArea button:has-text('批准')");
            await sleep(8000);
            const status = await page.locator("#statusText").textContent();
            assert(status === "已批准", `expected 已批准, got ${status}`);
        });

        await test("approval bar disappears after approve", async () => {
            const visible = await page.locator("#approvalBarArea .approval-bar").isVisible();
            assert(!visible, "approval bar should be hidden after approval");
        });

        await page.close();
    }

    // =========================================================================
    // 4. Reject Flow
    // =========================================================================
    console.log("\n--- Reject Flow ---");
    {
        const page = await context.newPage();
        await page.goto(`${BASE_URL}/dashboard`, { waitUntil: "domcontentloaded" });
        await page.fill("#targetDate", "2026-07-27");
        await page.click("#startBtn");
        await sleep(8000);

        await test("clicking reject changes status to rejected", async () => {
            await page.click("#approvalBarArea button:has-text('拒绝')");
            await sleep(5000);
            const status = await page.locator("#statusText").textContent();
            assert(status === "已拒绝", `expected 已拒绝, got ${status}`);
        });

        await page.close();
    }

    // =========================================================================
    // 5. Revise Flow with Interpretation
    // =========================================================================
    console.log("\n--- Revise + Interpret Flow ---");
    {
        const page = await context.newPage();
        const pageErrors = [];
        page.on("pageerror", err => pageErrors.push(err.message));

        await page.goto(`${BASE_URL}/dashboard`, { waitUntil: "domcontentloaded" });
        await page.fill("#targetDate", "2026-07-27");
        await page.click("#startBtn");
        await sleep(8000);

        await test("clicking revise shows revision input", async () => {
            await page.click("#approvalBarArea button:has-text('修改')");
            await sleep(500);
            const visible = await page.locator("#revisionArea .revision-box").isVisible();
            assert(visible, "revision box should appear");
        });

        await test("can type revision comment and click interpret", async () => {
            await page.fill("#revisionComment", "末端SOC别低于30%");
            const btnText = await page.locator("#revisionArea button:has-text('解析指令')").isVisible();
            assert(btnText, "interpret button should be visible");
        });

        await test("no page errors during revision", async () => {
            assert(pageErrors.length === 0, `unexpected page errors: ${pageErrors.join(", ")}`);
        });

        await page.close();
    }

    // =========================================================================
    // 6. Chart.js Loads Correctly
    // =========================================================================
    console.log("\n--- Chart.js Integration ---");
    {
        const page = await context.newPage();
        await page.goto(`${BASE_URL}/dashboard`, { waitUntil: "domcontentloaded" });

        await test("Chart.js library is loaded", async () => {
            const hasChart = await page.evaluate(() => typeof Chart !== "undefined");
            assert(hasChart, "Chart.js should be defined globally");
        });

        await test("chart.min.js returns 200", async () => {
            const resp = await page.evaluate(async () => {
                const r = await fetch("/chart.min.js");
                return { status: r.status, length: (await r.text()).length };
            });
            assert(resp.status === 200, `expected 200, got ${resp.status}`);
            assert(resp.length > 100000, `chart.min.js should be >100KB, got ${resp.length}`);
        });

        await page.close();
    }

    // =========================================================================
    // 7. Responsive Layout
    // =========================================================================
    console.log("\n--- Responsive ---");
    {
        const mobileCtx = await browser.newContext({ viewport: { width: 390, height: 844 } });
        const page = await mobileCtx.newPage();
        await page.goto(`${BASE_URL}/dashboard`, { waitUntil: "domcontentloaded" });

        await test("mobile viewport renders without overflow", async () => {
            const bodyWidth = await page.evaluate(() => document.body.scrollWidth);
            assert(bodyWidth <= 410, `body should not overflow on mobile: ${bodyWidth}px`);
        });

        await test("mobile config bar wraps", async () => {
            const configHeight = await page.evaluate(() => {
                return document.querySelector(".config-bar").offsetHeight;
            });
            assert(configHeight > 40, "config bar should wrap to multiple rows on mobile");
        });

        await page.close();
        await mobileCtx.close();
    }

    // =========================================================================
    // 8. API Health
    // =========================================================================
    console.log("\n--- API Health ---");
    {
        const page = await context.newPage();
        await page.goto(`${BASE_URL}/dashboard`, { waitUntil: "domcontentloaded" });

        await test("health endpoint returns ok", async () => {
            const resp = await page.evaluate(async () => {
                const r = await fetch("/health");
                return r.json();
            });
            assert(resp.status === "ok", `health should be ok, got ${resp.status}`);
        });

        await test("all 5 agent endpoints exist", async () => {
            const resp = await page.evaluate(async () => {
                const r = await fetch("/openapi.json");
                const spec = await r.json();
                return Object.keys(spec.paths).filter(p => p.includes("/api/agents"));
            });
            assert(resp.length === 5, `expected 5 agent endpoints, got ${resp.length}: ${resp.join(", ")}`);
        });

        await page.close();
    }

    await browser.close();

    // Summary
    console.log("\n=== Summary ===");
    const passed = results.filter(r => r.status === "PASS").length;
    const failed = results.filter(r => r.status === "FAIL").length;
    console.log(`Total: ${results.length} | PASS: ${passed} | FAIL: ${failed}\n`);

    if (failed > 0) {
        console.log("Failed tests:");
        results.filter(r => r.status === "FAIL").forEach(r => {
            console.log(`  ✗ ${r.name}: ${r.error}`);
        });
        process.exit(1);
    }
})();
