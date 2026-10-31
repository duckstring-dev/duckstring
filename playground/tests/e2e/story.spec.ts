import { test, expect, type Page } from '@playwright/test';
import { STORY_LINES } from '../../src/lib/story';

// The story (/story, embedded on the landing page) plays every line in order and ends on its two buttons, at four
// times speed. Each line is checked on screen both centred and docked, and a screenshot per line lands in
// test-results/ for review.

async function onScreen(page: Page, testId: string, what: string) {
  const box = await page.getByTestId(testId).boundingBox();
  const vp = page.viewportSize()!;
  expect(box, what).not.toBeNull();
  expect(box!.x, what).toBeGreaterThanOrEqual(-0.5);
  expect(box!.y, what).toBeGreaterThanOrEqual(-0.5);
  expect(box!.x + box!.width, what).toBeLessThanOrEqual(vp.width + 0.5);
  expect(box!.y + box!.height, what).toBeLessThanOrEqual(vp.height + 0.5);
}

test('the story plays end to end', async ({ page }, info) => {
  const errors: string[] = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await page.goto('/story?autoplay=1&rate=4');
  const caption = page.getByTestId('story-caption');
  const lines = Object.entries(STORY_LINES);
  for (const [i, [key, line]] of lines.entries()) {
    await expect(caption).toHaveText(line, { timeout: 30_000 });
    await page.waitForTimeout(250); // faded in, centred
    await onScreen(page, 'story-caption', `${key} centred`);
    await page.screenshot({ path: info.outputPath(`${String(i + 1).padStart(2, '0')}-${key}.png`) });
  }
  await expect(page.getByTestId('story')).toHaveAttribute('data-phase', 'ended', { timeout: 30_000 });
  await expect(page.getByTestId('story-replay')).toBeVisible();
  await onScreen(page, 'story-replay', 'replay button');
  expect(errors).toEqual([]);
});

test('a line docks into the band, clear of the middle of the frame', async ({ page }) => {
  await page.goto('/story?autoplay=1&rate=4');
  const caption = page.getByTestId('story-caption');
  await expect(caption).toHaveText(STORY_LINES.shared, { timeout: 30_000 });
  // Centred, then docked once it has been read.
  await expect.poll(async () => {
    const b = (await caption.boundingBox())!;
    return b.y + b.height / 2 > page.viewportSize()!.height * 0.75;
  }, { timeout: 10_000 }).toBe(true);
  await onScreen(page, 'story-caption', 'docked');
});

test('without autoplay it waits for Play, and pause stops it', async ({ page }) => {
  await page.goto('/story?rate=4');
  const story = page.getByTestId('story');
  await expect(story).toHaveAttribute('data-phase', 'ready');
  await page.waitForTimeout(600);
  await expect(page.getByTestId('story-caption')).toHaveText('');
  await page.getByTestId('story-play').click();
  await expect(page.getByTestId('story-caption')).toHaveText(STORY_LINES.question);
  await page.getByTestId('story-pause').click();
  await page.waitForTimeout(3000); // the question would have given way by now at 4x
  await expect(page.getByTestId('story-caption')).toHaveText(STORY_LINES.question);
  await page.getByTestId('story-pause').click();
  await expect(page.getByTestId('story-caption')).toHaveText(STORY_LINES.shared, { timeout: 10_000 });
});

test('skip moves to the next scene and back returns to the one before', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await page.goto('/story?autoplay=1&rate=4');
  const caption = page.getByTestId('story-caption');
  await expect(caption).toHaveText(STORY_LINES.question);
  await page.getByTestId('story-skip').click();
  await expect(caption).toHaveText(STORY_LINES.continuous, { timeout: 10_000 });
  await page.getByTestId('story-skip').click();
  await expect(caption).toHaveText(STORY_LINES.breaking, { timeout: 10_000 });
  // Just begun: back goes to the scene before.
  await page.getByTestId('story-back').click();
  await expect(caption).toHaveText(STORY_LINES.continuous, { timeout: 10_000 });
  // Well into it: back restarts it.
  await expect(caption).toHaveText(STORY_LINES.paced, { timeout: 30_000 });
  await page.getByTestId('story-back').click();
  await expect(caption).toHaveText(STORY_LINES.continuous, { timeout: 10_000 });
  // The arrow keys do the same.
  await page.keyboard.press('ArrowRight');
  await expect(caption).toHaveText(STORY_LINES.breaking, { timeout: 10_000 });
  expect(errors).toEqual([]);
});

test('skipping the last scene ends the story, with the Playground explained and a way home', async ({ page }, info) => {
  await page.goto('/story?autoplay=1&rate=4');
  const caption = page.getByTestId('story-caption');
  await expect(caption).toHaveText(STORY_LINES.question);
  for (let i = 0; i < 4; i++) await page.getByTestId('story-skip').click();
  await expect(page.getByTestId('story')).toHaveAttribute('data-phase', 'ended', { timeout: 10_000 });
  await expect(caption).toHaveText(STORY_LINES.work);
  await expect(page.getByTestId('story-pitch')).toBeVisible();
  await expect(page.getByTestId('story-home')).toHaveAttribute('href', 'https://duckstring.com');
  for (const id of ['story-pitch', 'story-replay', 'story-home']) await onScreen(page, id, id);
  await page.waitForTimeout(600); // the buttons fade in
  await page.screenshot({ path: info.outputPath('end.png') });
  // Back from the end replays the last scene.
  await page.getByTestId('story-back').click();
  await expect(caption).toHaveText(STORY_LINES.trickle, { timeout: 10_000 });
});
