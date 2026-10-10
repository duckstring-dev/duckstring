import { test, expect, type Page } from '@playwright/test';
import { TOUR_STEPS } from '../../src/lib/tourSteps';

// Runs the whole tour with the primary button on every step, checking that each step opens in
// order, that every wait ends on its condition rather than its fallback timer, and that the card
// stays on screen. A screenshot per step lands in test-results/ for review.

async function onScreen(page: Page, selector: string) {
  const box = await page.locator(selector).boundingBox();
  const vp = page.viewportSize()!;
  expect(box).not.toBeNull();
  expect(box!.x).toBeGreaterThanOrEqual(0);
  expect(box!.y).toBeGreaterThanOrEqual(0);
  expect(box!.x + box!.width).toBeLessThanOrEqual(vp.width + 0.5);
  expect(box!.y + box!.height).toBeLessThanOrEqual(vp.height + 0.5);
}

test('the tour runs end to end', async ({ page }, info) => {
  await page.goto('/?tour=1');
  const card = page.getByTestId('tour-card');

  for (let i = 0; i < TOUR_STEPS.length; i++) {
    const step = TOUR_STEPS[i];
    await expect(card).toHaveAttribute('data-step', step.id, { timeout: 30_000 });
    if (i > 0 && TOUR_STEPS[i - 1].advance !== 'next') {
      await expect(card).toHaveAttribute('data-last-advance', 'until');
    }
    await onScreen(page, '[data-testid="tour-card"]');
    if (step.target?.control) {
      // The outlined control is in view, and not under the card.
      const control = page.locator(`[data-tour="${step.target.control}"].ds-tour-outline`).first();
      await expect(control).toBeVisible();
      await expect(control).toBeInViewport();
      const [c, k] = [await control.boundingBox(), await card.boundingBox()];
      const overlap = c!.y < k!.y + k!.height && c!.y + c!.height > k!.y && c!.x < k!.x + k!.width && c!.x + c!.width > k!.x;
      expect(overlap, `${step.id}: the card covers its control`).toBe(false);
    }
    await page.waitForTimeout(500); // let the framing animation finish before the screenshot
    await page.screenshot({ path: info.outputPath(`${String(i + 1).padStart(2, '0')}-${step.id}.png`) });
    await page.getByTestId('tour-next').click();
  }

  await expect(card).toHaveCount(0);
  await expect(page.getByTestId('tour-pill')).toHaveCount(0);
  expect(await page.evaluate(() => localStorage.getItem('ds-playground-tour'))).toBe('done');
});

test('a first visit is offered the tour once', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByTestId('tour-prompt')).toBeVisible();
  await page.getByTestId('tour-decline').click();
  await expect(page.getByTestId('tour-prompt')).toHaveCount(0);
  await page.reload();
  await page.waitForTimeout(500);
  await expect(page.getByTestId('tour-prompt')).toHaveCount(0);
  // The Tour button restarts it at any time.
  await page.getByTestId('tour-button').click();
  await expect(page.getByTestId('tour-card')).toHaveAttribute('data-step', 'pipeline');
});

test('?tour=0 suppresses the prompt', async ({ page }) => {
  await page.goto('/?tour=0');
  await page.waitForTimeout(500);
  await expect(page.getByTestId('tour-prompt')).toHaveCount(0);
});

test('blocked storage still offers and runs the tour', async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(window, 'localStorage', {
      get() {
        throw new DOMException('blocked', 'SecurityError');
      },
    });
  });
  await page.goto('/');
  await page.getByTestId('tour-start').click();
  await expect(page.getByTestId('tour-card')).toHaveAttribute('data-step', 'pipeline');
  await page.getByTestId('tour-skip').click();
  await expect(page.getByTestId('tour-card')).toHaveCount(0);
});

test('starting the tour over a changed graph asks first', async ({ page }) => {
  await page.goto('/?tour=0');
  if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByTestId('tour-button').locator('..').click();
  await page.getByRole('button', { name: '+ Add Pond' }).click();
  await page.getByTestId('tour-button').click();
  await expect(page.getByTestId('tour-confirm')).toBeVisible();
  await page.getByTestId('tour-start').click();
  await expect(page.getByTestId('tour-card')).toHaveAttribute('data-step', 'pipeline');
});
