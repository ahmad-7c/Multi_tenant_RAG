import { defineConfig, devices } from "@playwright/test";

/**
 * End-to-end tests against the REAL stack: a real Next server, a real
 * FastAPI, a real Postgres with pgvector.
 *
 * Providers are FAKE (see scripts/e2e_env.sh) and the database is a
 * separate `documind_e2e`. Two reasons the dev database cannot be
 * used: the startup guard refuses to serve queries when stored
 * vectors came from a different embedding model than the configured
 * one, and tests here delete documents.
 *
 * The API is NOT started by Playwright. It needs a bash script to load
 * its environment and run migrations first, so it is started once by
 * hand (scripts/e2e_api.sh) and reused -- which also keeps a failing
 * test run from leaving a half-migrated database behind.
 */
export default defineConfig({
  testDir: "./e2e",
  // Serial. The suite shares one database and one organization per
  // test file; parallel workers would race each other's documents and
  // produce failures that depend on scheduling rather than on code.
  fullyParallel: false,
  workers: 1,
  // A failing assertion here usually means a real regression, and a
  // retry would hide a flaky stream rather than expose it.
  retries: 0,
  reporter: [["list"]],
  // Generous, because this runs against `next dev`: Turbopack compiles
  // each route the first time a test visits it, so the first
  // navigation to /documents or /usage pays a one-off cost that has
  // nothing to do with the behaviour under test.
  timeout: 90_000,
  expect: { timeout: 15_000 },
  use: {
    baseURL: "http://localhost:3020",
    trace: "retain-on-failure",
    // Deliberately NOT ignoring HTTPS errors or granting permissions:
    // the point is to exercise what a browser actually does.
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    // A PRODUCTION build, not `next dev`.
    //
    // Turbopack compiles each route on its first visit in dev, so the
    // first navigation to /documents or /usage paid a multi-second
    // cost that had nothing to do with the behaviour under test -- and
    // in a long serial run it made upload waits flaky, passing in
    // isolation and failing in the suite. Building once up front costs
    // ~30s and makes every page instant and deterministic. It also
    // means the tests exercise the bundle that would actually ship.
    command: "npx next build && npx next start -p 3020",
    url: "http://localhost:3020/login",
    reuseExistingServer: true,
    timeout: 300_000,
    env: {
      // The e2e API, not the one the developer is using on 8010.
      BACKEND_URL: "http://127.0.0.1:8020",
    },
  },
});
