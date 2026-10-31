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
  // Autoplay starts past the opening question (the landing page's poster shows it).
  const lines = Object.entries(STORY_LINES).filter(([key]) => key !== 'question');
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

test('without autoplay it says what it is and waits for Play, and pause stops it', async ({ page }, info) => {
  await page.goto('/story?rate=4');
  const story = page.getByTestId('story');
  await expect(story).toHaveAttribute('data-phase', 'ready');
  await expect(page.getByTestId('story-opening')).toHaveText(STORY_LINES.question);
  await onScreen(page, 'story-opening', 'opening question');
  await expect(page.getByTestId('story-label')).toContainText('DuckDB Data Engineering');
  await onScreen(page, 'story-label', 'who it is for');
  await onScreen(page, 'story-play', 'play button');
  await page.screenshot({ path: info.outputPath('ready.png') });
  await page.waitForTimeout(600);
  await expect(page.getByTestId('story-caption')).toHaveText('');
  // Play goes past the question just read, straight to the next line.
  await page.getByTestId('story-play').click();
  await expect(page.getByTestId('story-caption')).toHaveText(STORY_LINES.shared);
  await page.getByTestId('story-pause').click();
  await page.waitForTimeout(3000); // the line would have given way by now at 4x
  await expect(page.getByTestId('story-caption')).toHaveText(STORY_LINES.shared);
  await page.getByTestId('story-pause').click();
  await expect(page.getByTestId('story-caption')).toHaveText(STORY_LINES.rates, { timeout: 10_000 });
});

test('skip and back step one line at a time', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await page.goto('/story?autoplay=1&rate=4');
  const caption = page.getByTestId('story-caption');
  await expect(caption).toHaveText(STORY_LINES.shared);
  await page.getByTestId('story-skip').click();
  await expect(caption).toHaveText(STORY_LINES.rates);
  await page.getByTestId('story-skip').click();
  await expect(caption).toHaveText(STORY_LINES.supply);
  await page.getByTestId('story-back').click();
  await expect(caption).toHaveText(STORY_LINES.rates);
  await page.getByTestId('story-back').click();
  await expect(caption).toHaveText(STORY_LINES.shared);
  // At the first line, back starts it again.
  await page.getByTestId('story-back').click();
  await expect(caption).toHaveText(STORY_LINES.shared);
  // Docked, a line is still the one back and skip count from.
  await expect(caption).toHaveText(STORY_LINES.rates, { timeout: 30_000 });
  await expect(caption).toHaveText(STORY_LINES.supply, { timeout: 30_000 });
  await page.getByTestId('story-back').click();
  await expect(caption).toHaveText(STORY_LINES.rates);
  // The arrow keys do the same.
  await page.keyboard.press('ArrowRight');
  await expect(caption).toHaveText(STORY_LINES.supply);
  await page.keyboard.press('ArrowLeft');
  await expect(caption).toHaveText(STORY_LINES.rates);
  expect(errors).toEqual([]);
});

test('tapping skip shows every line in turn, then the end, with the Playground explained and a way home', async ({ page }, info) => {
  const errors: string[] = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await page.goto('/story?autoplay=1&rate=4');
  const caption = page.getByTestId('story-caption');
  const lines = Object.values(STORY_LINES).filter((l) => l !== STORY_LINES.question);
  await expect(caption).toHaveText(lines[0]);
  for (const line of lines.slice(1)) {
    await page.getByTestId('story-skip').click();
    await expect(caption).toHaveText(line);
  }
  await page.getByTestId('story-skip').click();
  await expect(page.getByTestId('story')).toHaveAttribute('data-phase', 'ended');
  await expect(caption).toHaveText(STORY_LINES.work);
  await expect(page.getByTestId('story-pitch')).toBeVisible();
  await expect(page.getByTestId('story-home')).toHaveAttribute('href', 'https://duckstring.com');
  for (const id of ['story-pitch', 'story-replay', 'story-home']) await onScreen(page, id, id);
  await page.waitForTimeout(600); // the buttons fade in
  await page.screenshot({ path: info.outputPath('end.png') });
  // Back from the end goes to the line before the last.
  await page.getByTestId('story-back').click();
  await expect(caption).toHaveText(STORY_LINES.trickle);
  // Watch again opens with the question.
  await page.getByTestId('story-skip').click();
  await page.getByTestId('story-skip').click();
  await page.getByTestId('story-replay').click();
  await expect(caption).toHaveText(STORY_LINES.question, { timeout: 10_000 });
  await expect(caption).toHaveText(STORY_LINES.shared, { timeout: 10_000 });
  expect(errors).toEqual([]);
});

// The docked caption sits between the back and skip buttons, beside (wide) or under (tall) the run chart.
test('back and skip sit in the bottom corners, either side of the caption', async ({ page }, info) => {
  await page.goto('/story?autoplay=1&rate=4');
  const caption = page.getByTestId('story-caption');
  const vp = page.viewportSize()!;
  const apart = (a: { x: number; y: number; width: number; height: number }, b: typeof a) =>
    a.x + a.width <= b.x || b.x + b.width <= a.x || a.y + a.height <= b.y || b.y + b.height <= a.y;
  const docked = async (line: string) => {
    await expect(caption).toHaveText(line, { timeout: 30_000 });
    await expect.poll(async () => {
      const b = (await caption.boundingBox())!;
      return b.y + b.height / 2 > vp.height * 0.6;
    }, { timeout: 10_000 }).toBe(true);
    await page.waitForTimeout(300); // the dock's slide settles (650 ms, at 4x)
  };
  const corners = async () => {
    const back = (await page.getByTestId('story-back').boundingBox())!;
    const skip = (await page.getByTestId('story-skip').boundingBox())!;
    const cap = (await caption.boundingBox())!;
    expect(back.x).toBeLessThan(vp.width * 0.2);
    expect(skip.x + skip.width).toBeGreaterThan(vp.width * 0.8);
    for (const b of [back, skip]) {
      expect(b.y + b.height).toBeGreaterThan(vp.height - 90);
      expect(apart(b, cap), 'button clear of the caption').toBe(true);
    }
    // Tall, between them, not above them (wide, it centres beside the run chart when that's up).
    if (vp.height > vp.width) expect(cap.y + cap.height).toBeGreaterThan(back.y);
    return { back, skip, cap };
  };

  // The supply line docks with the run chart: the fullest the band gets.
  await docked(STORY_LINES.supply);
  const { back, skip, cap } = await corners();
  const chart = (await page.getByTestId('story-chart').boundingBox())!;
  for (const b of [back, skip, cap]) expect(apart(b, chart), 'clear of the chart').toBe(true);
  await page.screenshot({ path: info.outputPath('corners.png') });

  // The longest line.
  for (let i = 0; i < 12 && (await caption.textContent()) !== STORY_LINES.trickle; i++) {
    await page.getByTestId('story-skip').click();
    await page.waitForTimeout(100);
  }
  await docked(STORY_LINES.trickle);
  await corners();
  await page.screenshot({ path: info.outputPath('corners-longest.png') });
});
