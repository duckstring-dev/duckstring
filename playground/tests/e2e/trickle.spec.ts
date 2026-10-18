import { test, expect, type Page } from '@playwright/test';
import { TRICKLE_STEPS } from '../../src/lib/trickle/tour';

// /trickle at every viewport: the tour runs end to end (its second chapter runs the pipeline to each moment
// and pauses there), the card stays on screen, and after the tour the pipeline keeps running. A screenshot
// per step lands in test-results/ for review.

async function onScreen(page: Page, selector: string) {
  const box = await page.locator(selector).boundingBox();
  const vp = page.viewportSize()!;
  expect(box).not.toBeNull();
  expect(box!.x).toBeGreaterThanOrEqual(0);
  expect(box!.y).toBeGreaterThanOrEqual(0);
  expect(box!.x + box!.width).toBeLessThanOrEqual(vp.width + 0.5);
  expect(box!.y + box!.height).toBeLessThanOrEqual(vp.height + 0.5);
}

test('the Trickle tour runs end to end', async ({ page }, info) => {
  await page.goto('/trickle?tour=1');
  const card = page.getByTestId('trickle-tour');
  const phase = page.getByTestId('trickle-phase');
  // Where chapter 2's steps pause the pipeline.
  const stops: Record<string, string> = {
    pipeline: 'run 1 · done · paused',
    arrive: 'run 2 · sources change · paused',
    join: 'run 2 · priced joins the changes · paused',
    aggregate: 'run 2 · done · paused',
    fold: 'run 3 · sources change · paused',
  };
  for (let i = 0; i < TRICKLE_STEPS.length; i++) {
    const step = TRICKLE_STEPS[i];
    await expect(card).toHaveAttribute('data-step', step.id);
    await onScreen(page, '[data-testid="trickle-tour"]');
    if (stops[step.id]) await expect(phase).toHaveText(stops[step.id], { timeout: 30_000 });
    if (step.id === 'join') await expect(card).toContainText(/took part when producing priced: \d+ rows, \d+% of the 1,050/);
    await page.waitForTimeout(300);
    await page.screenshot({ path: info.outputPath(`${String(i + 1).padStart(2, '0')}-${step.id}.png`) });
    await expect(page.getByTestId('trickle-tour-next')).toBeInViewport({ ratio: 1 });
    await page.getByTestId('trickle-tour-next').click();
  }
  await expect(card).toHaveCount(0);
  // After the tour the pipeline runs on by itself.
  await expect(phase).toHaveText(/run 4/, { timeout: 30_000 });
  const [scroll, client] = await page.evaluate(() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]);
  expect(scroll).toBeLessThanOrEqual(client);
});

test('without the tour the pipeline runs, and a removal reaches priced', async ({ page }, info) => {
  await page.goto('/trickle?tour=0');
  await expect(page.getByTestId('trickle-tour')).toHaveCount(0);
  await page.getByText('1/2').waitFor();
  await page.getByRole('button', { name: '2x' }).click();
  await expect(page.getByTestId('trickle-phase')).toHaveText(/run 2/, { timeout: 30_000 });
  // The toggles are in the bottom sheet on a phone.
  if (!(await page.getByRole('switch', { name: 'Remove a product' }).isVisible())) await page.getByTestId('trickle-sheet').click();
  await page.getByRole('switch', { name: 'Remove a product' }).click();
  await expect(page.getByTestId('node-priced-status')).toContainText('−', { timeout: 30_000 });
  await page.screenshot({ path: info.outputPath('removal.png') });
});

test('one page switches between orchestration and Trickle, and each keeps its state', async ({ page }) => {
  await page.goto('/?tour=0');
  await page.getByTestId('mode-trickle').first().click();
  await expect(page).toHaveURL(/\/trickle$/);
  await expect(page.getByTestId('node-priced')).toBeVisible();
  // First visit to Trickle mode opens its tour; skip it and let the pipeline run to its second run.
  await page.getByTestId('trickle-tour-skip').click();
  await page.getByRole('button', { name: '2x' }).click();
  await expect(page.getByTestId('trickle-phase')).toHaveText(/run [2-9]/, { timeout: 30_000 });
  await page.getByTestId('mode-orchestration').first().click();
  await expect(page).toHaveURL(/\/$/);
  await expect(page.locator('#ds-canvas')).toBeVisible();
  await page.getByTestId('mode-trickle').first().click();
  // Back in Trickle mode the pipeline carries on from where it was, without the tour.
  await expect(page.getByTestId('trickle-tour')).toHaveCount(0);
  await expect(page.getByTestId('trickle-phase')).toHaveText(/run [2-9]/);
});

test('the Trickle tour hands over to the orchestration tour', async ({ page }) => {
  await page.goto('/trickle?tour=1');
  for (let i = 0; i < TRICKLE_STEPS.length - 1; i++) await page.getByTestId('trickle-tour-next').click();
  await expect(page.getByTestId('trickle-tour')).toHaveAttribute('data-step', 'try');
  await page.getByTestId('trickle-tour-mode-orchestration').click();
  await expect(page).toHaveURL(/\/$/);
  await expect(page.locator('#ds-canvas')).toBeVisible();
  await expect(page.getByTestId('tour-prompt')).toBeVisible(); // a first visit to orchestration offers its tour
});
