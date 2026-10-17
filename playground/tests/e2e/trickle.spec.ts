import { test, expect, type Page } from '@playwright/test';

// The /trickle page at every viewport: no sideways scroll, each figure responds, and the pipeline runs. A
// full-page screenshot per stage lands in test-results/ for review.

async function noSideScroll(page: Page) {
  const [scroll, client] = await page.evaluate(() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]);
  expect(scroll).toBeLessThanOrEqual(client);
}

test('the Trickle page works through all four figures', async ({ page }, info) => {
  await page.goto('/trickle');
  await expect(page.getByRole('heading', { level: 1, name: 'Trickle' })).toBeVisible();
  await noSideScroll(page);
  await page.screenshot({ path: info.outputPath('01-top.png'), fullPage: true });

  // The join grid builds up its three terms.
  const eq = page.getByTestId('join-eq');
  await expect(eq).toHaveText('Δ(A ⋈ B) = ?');
  for (let i = 0; i < 3; i++) await page.getByTestId('join-next').click();
  await expect(eq).toHaveText('Δ(A ⋈ B) = ΔA ⋈ B′ + A′ ⋈ ΔB − ΔA ⋈ ΔB');

  // Narrowing by key leaves a small part of each table.
  const orders = page.getByTestId('keys-orders');
  await expect(orders).toHaveText(/^492 of 492/);
  await page.getByTestId('keys-narrow').click();
  const narrowed = Number((await orders.textContent())!.split(' ')[0]);
  expect(narrowed).toBeGreaterThan(0);
  expect(narrowed).toBeLessThan(100);

  // The pipeline: a first run computes everything, later runs a small part.
  const run = page.getByTestId('trickle-run');
  await run.scrollIntoViewIfNeeded();
  await run.click();
  await expect(page.getByTestId('trickle-run-label')).toHaveText('after run 1');
  await expect(page.getByTestId('pond-orders-counts')).toHaveText('2,000 rows, all new');
  await page.screenshot({ path: info.outputPath('02-run-1.png'), fullPage: true });
  await run.click();
  await expect(page.getByTestId('trickle-run-label')).toHaveText('after run 2');
  await expect(page.getByTestId('pond-orders-counts')).toContainText('40 added');
  await expect(page.getByTestId('trickle-summary')).toContainText('took part');
  await page.screenshot({ path: info.outputPath('03-run-2.png'), fullPage: true });
  await page.getByRole('switch', { name: 'Remove a product' }).click();
  await run.click();
  await expect(page.getByTestId('trickle-run-label')).toHaveText('after run 3');
  await expect(page.getByTestId('pond-priced-counts')).toContainText('removed');
  await noSideScroll(page);
  await page.screenshot({ path: info.outputPath('04-run-3.png'), fullPage: true });
});

test('the Playground links to the Trickle page', async ({ page }) => {
  await page.goto('/?tour=0');
  await page.getByTestId('trickle-link').first().click();
  await expect(page).toHaveURL(/\/trickle$/);
  await expect(page.getByRole('heading', { level: 1, name: 'Trickle' })).toBeVisible();
});
