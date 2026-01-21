import fs from "node:fs";
import path from "node:path";
import { chromium } from "playwright";
import { JSDOM } from "jsdom";
import { Readability } from "@mozilla/readability";

const url = process.env.TARGET_URL || process.env.TARGET_URL;
if (!process.env.TARGET_URL) throw new Error("Set TARGET_URL");

fs.mkdirSync("output", { recursive: true });

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({
  userAgent:
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
});

await page.goto(process.env.TARGET_URL, { waitUntil: "networkidle", timeout: 120000 });

// اگر lazy-load دارند، کمی اسکرول
await page.evaluate(async () => {
  window.scrollTo(0, document.body.scrollHeight);
  await new Promise(r => setTimeout(r, 1200));
});

const html = await page.content();

const dom = new JSDOM(html, { url: process.env.TARGET_URL });
const reader = new Readability(dom.window.document);
const article = reader.parse();

const out = {
  url: process.env.TARGET_URL,
  title: article?.title || null,
  text: article?.textContent || null
};

fs.writeFileSync(path.join("output", "result.json"), JSON.stringify(out, null, 2), "utf8");

await browser.close();
