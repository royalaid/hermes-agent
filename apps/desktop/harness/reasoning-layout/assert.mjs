import { chromium } from '@playwright/test'

const url = process.argv[2] ?? 'http://127.0.0.1:5174/harness/reasoning-layout/'
const screenshotPath = process.argv[3]
const browser = await chromium.launch({ headless: true })
const page = await browser.newPage({ viewport: { width: 900, height: 600 } })
page.setDefaultTimeout(120_000)

try {
  await page.goto(url, { timeout: 120_000, waitUntil: 'domcontentloaded' })
  const disclosure = page.locator('[data-slot="aui_thinking-disclosure"]')
  await disclosure.getByRole('button').click()

  const parts = disclosure.locator('[data-slot="aui_reasoning-text"]')
  const count = await parts.count()
  const rows = await parts.evaluateAll(nodes =>
    nodes.map(node => {
      const rect = node.getBoundingClientRect()
      return {
        display: getComputedStyle(node).display,
        text: node.textContent?.trim() ?? '',
        top: rect.top
      }
    })
  )

  if (count !== 4) {
    throw new Error(`Expected 4 reasoning items, found ${count}: ${JSON.stringify(rows)}`)
  }

  if (rows.some(row => row.display !== 'block')) {
    throw new Error(`Every reasoning item must explicitly render as display:block: ${JSON.stringify(rows)}`)
  }

  if (new Set(rows.map(row => Math.round(row.top))).size !== rows.length) {
    throw new Error(`Reasoning items do not occupy distinct visual rows: ${JSON.stringify(rows)}`)
  }

  if (screenshotPath) {
    await page.screenshot({ path: screenshotPath })
  }

  console.log(JSON.stringify({ count, rows }, null, 2))
} finally {
  await browser.close()
}
