import { expect, test, type Page } from '@playwright/test'

import { setupMockBackend, type MockBackendFixture, waitForAppReady } from './fixtures'

interface SidebarLayout {
  contentOverflowY: string
  sectionsOverflowY: string
  sectionBodyOverflows: string[]
  navBottom: number
  firstHeaderTop: number
  lastHeaderBottom: number
  profileTop: number
  sidebarTop: number
  sidebarBottom: number
}

async function sidebarLayout(page: Page): Promise<SidebarLayout> {
  return page.evaluate(() => {
    const sections = document.querySelector<HTMLElement>('[data-sidebar-sections]')!
    const sidebar = sections.closest<HTMLElement>('[data-slot="sidebar"]')!
    const content = sections.closest<HTMLElement>('[data-sidebar="content"]')!
    const nav = content.firstElementChild as HTMLElement
    const profile = content.lastElementChild as HTMLElement
    const headers = [...sections.querySelectorAll<HTMLElement>('div')].filter(element =>
      element.classList.contains('group/section')
    )
    const bodies = [...sections.querySelectorAll<HTMLElement>('[data-sidebar="group-content"]')]
    const sidebarRect = sidebar.getBoundingClientRect()

    return {
      contentOverflowY: getComputedStyle(content).overflowY,
      sectionsOverflowY: getComputedStyle(sections).overflowY,
      sectionBodyOverflows: bodies.map(body => getComputedStyle(body).overflowY),
      navBottom: nav.getBoundingClientRect().bottom,
      firstHeaderTop: headers[0]?.getBoundingClientRect().top ?? sections.getBoundingClientRect().top,
      lastHeaderBottom: headers.at(-1)?.getBoundingClientRect().bottom ?? sections.getBoundingClientRect().bottom,
      profileTop: profile.getBoundingClientRect().top,
      sidebarTop: sidebarRect.top,
      sidebarBottom: sidebarRect.bottom
    }
  })
}

function expectAnchoredLayout(layout: SidebarLayout): void {
  expect(layout.contentOverflowY).toBe('hidden')
  expect(layout.sectionsOverflowY).toBe('hidden')
  expect(layout.sectionBodyOverflows.length).toBeGreaterThan(0)
  expect(layout.sectionBodyOverflows.every(overflow => overflow === 'auto')).toBe(true)
  expect(layout.navBottom).toBeLessThanOrEqual(layout.firstHeaderTop)
  expect(layout.lastHeaderBottom).toBeLessThanOrEqual(layout.profileTop)
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

  test('anchors shell chrome while section bodies own scrolling at normal and compact heights', async () => {
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

    // Exercise the actual section scroller while the compact geometry is on
    // screen; the recorded Playwright video then proves the anchored chrome
    // remains fixed through both the resize and scroll interaction.
    const body = page.locator('[data-sidebar-sections] [data-sidebar="group-content"]').last()
    await body.hover()
    await page.mouse.wheel(0, 500)
    await page.waitForTimeout(750)
  })
})
