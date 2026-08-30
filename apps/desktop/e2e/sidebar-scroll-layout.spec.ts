import { expect, type Locator, type Page, test } from '@playwright/test'

import { type MockBackendFixture, setupMockBackend, waitForAppReady } from './fixtures'

interface SidebarLayout {
  contentOverflowY: string
  sectionsOverflowY: string
  sectionBodyOverflows: string[]
  navBottom: number
  sectionsTop: number
  sectionsBottom: number
  profileTop: number
  sidebarTop: number
  sidebarBottom: number
}

async function scrollMetrics(
  target: Locator
): Promise<{ clientHeight: number; scrollHeight: number; scrollTop: number }> {
  return target.first().evaluate(element => {
    let owner: HTMLElement | null = element as HTMLElement

    while (owner && !['auto', 'scroll'].includes(getComputedStyle(owner).overflowY)) {
      owner = owner.parentElement
    }

    if (!owner) {
      throw new Error('No vertical scroll owner found')
    }

    return { clientHeight: owner.clientHeight, scrollHeight: owner.scrollHeight, scrollTop: owner.scrollTop }
  })
}

async function resetScroll(target: Locator): Promise<void> {
  await target.first().evaluate(element => {
    let owner: HTMLElement | null = element as HTMLElement

    while (owner && !['auto', 'scroll'].includes(getComputedStyle(owner).overflowY)) {
      owner = owner.parentElement
    }

    owner?.scrollTo({ top: 0 })
  })
}

async function sidebarLayout(page: Page): Promise<SidebarLayout> {
  return page.evaluate(() => {
    const sections = document.querySelector<HTMLElement>('[data-sidebar-sections]')!
    const sidebar = sections.closest<HTMLElement>('[data-slot="sidebar"]')!
    const content = sections.closest<HTMLElement>('[data-sidebar="content"]')!
    const nav = content.firstElementChild as HTMLElement
    const profile = content.lastElementChild as HTMLElement
    const bodies = [...sections.querySelectorAll<HTMLElement>('[data-sidebar="group-content"]')]
    const sidebarRect = sidebar.getBoundingClientRect()
    const sectionsRect = sections.getBoundingClientRect()

    return {
      contentOverflowY: getComputedStyle(content).overflowY,
      sectionsOverflowY: getComputedStyle(sections).overflowY,
      sectionBodyOverflows: bodies.map(body => getComputedStyle(body).overflowY),
      navBottom: nav.getBoundingClientRect().bottom,
      sectionsTop: sectionsRect.top,
      sectionsBottom: sectionsRect.bottom,
      profileTop: profile.getBoundingClientRect().top,
      sidebarTop: sidebarRect.top,
      sidebarBottom: sidebarRect.bottom
    }
  })
}

function expectAnchoredLayout(layout: SidebarLayout): void {
  expect(layout.contentOverflowY).toBe('hidden')
  expect(layout.sectionsOverflowY).toBe('auto')
  expect(layout.sectionBodyOverflows.length).toBeGreaterThan(0)
  expect(layout.sectionBodyOverflows.every(overflow => overflow === 'visible')).toBe(true)
  expect(layout.navBottom).toBeLessThanOrEqual(layout.sectionsTop)
  expect(layout.sectionsBottom).toBeLessThanOrEqual(layout.profileTop)
  expect(layout.profileTop).toBeGreaterThanOrEqual(layout.sidebarTop)
  expect(layout.profileTop).toBeLessThan(layout.sidebarBottom)
}

test.describe('sidebar scroll containment', () => {
  test.setTimeout(180_000)

  let fixture: MockBackendFixture

  test.beforeAll(async () => {
    fixture = await setupMockBackend()
    await waitForAppReady(fixture, 120_000)
  })

  test.afterAll(async () => {
    await fixture?.cleanup()
  })

  test('anchors shell chrome while the section stack scrolls from headers and rows', async () => {
    const { app, page } = fixture

    const composer = page.locator('[contenteditable="true"]').first()
    await composer.click()
    await composer.fill('Create a sidebar session for the layout check')
    await page.keyboard.press('Enter')
    await page.locator('[data-sidebar-sections]').waitFor({ state: 'visible', timeout: 30_000 })

    await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]?.setSize(1220, 800))
    await page.waitForTimeout(250)
    expectAnchoredLayout(await sidebarLayout(page))

    await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]?.setSize(1000, 560))
    await page.waitForTimeout(250)
    expectAnchoredLayout(await sidebarLayout(page))

    // Force the section stack to overflow without changing list behavior.
    await page.locator('[data-sidebar-sections]').evaluate(sections => {
      const filler = document.createElement('div')
      filler.dataset.scrollFixture = ''
      filler.style.flex = '0 0 800px'
      sections.append(filler)
    })
    const row = page.locator('[data-sidebar-sections] [data-slot="row-button"]').last()
    const header = page.locator('[data-sidebar-sections] button').first()
    const beforeHeader = await scrollMetrics(header)

    expect(beforeHeader.scrollHeight).toBeGreaterThan(beforeHeader.clientHeight)
    await header.hover()
    await page.mouse.wheel(0, 500)
    await expect.poll(async () => (await scrollMetrics(header)).scrollTop).toBeGreaterThan(beforeHeader.scrollTop)

    await resetScroll(header)
    await row.hover()
    await page.mouse.wheel(0, 500)
    await expect.poll(async () => (await scrollMetrics(row)).scrollTop).toBeGreaterThan(0)
  })

  test('scrolls Skills and Tools over controls and keeps MCP viewport-bound', async () => {
    const { page } = fixture

    await page.getByRole('button', { name: 'Capabilities' }).click()
    const firstRow = page.getByRole('button', { name: /airtable Productivity/i })
    await firstRow.waitFor({ state: 'visible' })

    const beforeRow = await scrollMetrics(firstRow)

    expect(beforeRow.scrollHeight).toBeGreaterThan(beforeRow.clientHeight)
    await firstRow.hover()
    await page.mouse.wheel(0, 500)
    await expect.poll(async () => (await scrollMetrics(firstRow)).scrollTop).toBeGreaterThan(0)

    await resetScroll(firstRow)
    const firstSwitch = page.getByRole('switch', { name: 'airtable' })
    const beforeSwitch = await scrollMetrics(firstSwitch)

    await firstSwitch.hover()
    await page.mouse.wheel(0, 500)
    await expect.poll(async () => (await scrollMetrics(firstSwitch)).scrollTop).toBeGreaterThan(beforeSwitch.scrollTop)

    await page.getByRole('button', { name: /^Tools/ }).click()
    const firstToolSwitch = page.getByRole('switch').first()
    await firstToolSwitch.waitFor({ state: 'visible' })
    const beforeToolSwitch = await scrollMetrics(firstToolSwitch)

    expect(beforeToolSwitch.scrollHeight).toBeGreaterThan(beforeToolSwitch.clientHeight)
    await firstToolSwitch.hover()
    await page.mouse.wheel(0, 500)
    await expect.poll(async () => (await scrollMetrics(firstToolSwitch)).scrollTop).toBeGreaterThan(0)

    await page.getByRole('button', { name: /^MCP/ }).click()
    const catalogHeading = page.getByText('Catalog', { exact: true }).last()
    await catalogHeading.waitFor({ state: 'visible' })

    const mcpLayout = await catalogHeading.evaluate(element => {
      const pane = element.closest<HTMLElement>('[data-pane-content]')
      let list: HTMLElement | null = element.parentElement

      while (list && !['auto', 'scroll'].includes(getComputedStyle(list).overflowY)) {
        list = list.parentElement
      }

      if (!pane || !list) {
        throw new Error('MCP pane geometry is incomplete')
      }

      const listRect = list.getBoundingClientRect()
      const paneRect = pane.getBoundingClientRect()

      return {
        listBottom: listRect.bottom,
        listClientHeight: list.clientHeight,
        listOverflowY: getComputedStyle(list).overflowY,
        listTop: listRect.top,
        paneBottom: paneRect.bottom,
        paneClientHeight: pane.clientHeight,
        paneTop: paneRect.top
      }
    })

    expect(mcpLayout.listOverflowY).toBe('auto')
    expect(mcpLayout.listClientHeight).toBeGreaterThan(0)
    expect(mcpLayout.listClientHeight).toBeLessThanOrEqual(mcpLayout.paneClientHeight)
    expect(mcpLayout.listTop).toBeGreaterThanOrEqual(mcpLayout.paneTop)
    expect(mcpLayout.listBottom).toBeLessThanOrEqual(mcpLayout.paneBottom)
  })
})
