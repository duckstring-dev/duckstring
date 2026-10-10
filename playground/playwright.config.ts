import { defineConfig, devices } from '@playwright/test';

// Unit tests run in Node; the end-to-end tour runs against a production build at three viewports.
const PORT = 3123;

export default defineConfig({
  testDir: 'tests',
  outputDir: 'test-results',
  timeout: 180_000,
  use: { baseURL: `http://localhost:${PORT}` },
  projects: [
    { name: 'unit', testDir: 'tests/unit' },
    {
      name: 'phone-360',
      testDir: 'tests/e2e',
      use: { ...devices['Pixel 5'], viewport: { width: 360, height: 640 } },
    },
    {
      name: 'phone-390',
      testDir: 'tests/e2e',
      use: { ...devices['iPhone 13'], browserName: 'chromium', viewport: { width: 390, height: 844 } },
    },
    {
      name: 'desktop',
      testDir: 'tests/e2e',
      use: { ...devices['Desktop Chrome'], viewport: { width: 1280, height: 800 } },
    },
  ],
  webServer: {
    command: `npm run build && npx next start -p ${PORT}`,
    url: `http://localhost:${PORT}`,
    reuseExistingServer: !process.env.CI,
    timeout: 180_000,
  },
});
