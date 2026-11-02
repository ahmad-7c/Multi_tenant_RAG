/**
 * Apply ONE frontend mutation by name, so the harness stays readable.
 *
 * Same discipline as scripts/mutate.py on the backend: each mutation
 * is a plausible mistake a reviewer would wave through, not vandalism.
 * If the suite still passes with it applied, the test does not pin the
 * behaviour it claims to.
 *
 *     node e2e/mutate.mjs --list
 *     node e2e/mutate.mjs <name>          # apply
 *     node e2e/mutate.mjs <name> --revert # put it back
 */
import { readFileSync, writeFileSync } from "node:fs";

const MUTATIONS = {
  // U1: the session cookie stops being httpOnly, so script can read it.
  "u1-cookie-not-httponly": {
    file: "lib/session.ts",
    from: "    httpOnly: true,\n    // Off in development",
    to: "    httpOnly: false,\n    // Off in development",
    expect: "auth.spec.ts:2",
  },
  // U2: markdown is rendered without sanitisation.
  "u2-no-sanitize": {
    file: "components/app/chat/AnswerMarkdown.tsx",
    from: "        rehypePlugins={[[rehypeSanitize, schema]]}",
    to: "        rehypePlugins={[]}",
    expect: "app.spec.ts:8",
  },
  // U3: images are allowed back into answers.
  "u3-allow-images": {
    file: "components/app/chat/AnswerMarkdown.tsx",
    from: '    (tag) => tag !== "img" && tag !== "input",',
    to: '    (tag) => tag !== "input",',
    expect: "app.spec.ts:8",
  },
  // U4: the browser's cookie jar is forwarded upstream.
  "u4-forward-cookie": {
    file: "lib/proxy-rules.ts",
    from: '  "host",\n  "cookie",',
    to: '  "host",',
    expect: "bff.spec.ts:U4",
  },
  // U5: the proxy allowlist is removed; anything is reachable.
  "u5-no-allowlist": {
    file: "lib/proxy-rules.ts",
    from: "  if (!ALLOWED_PREFIXES.includes(segments[0])) return null;",
    to: "  // allowlist removed",
    expect: "bff.spec.ts:U5",
  },
  // U6: the CSRF check always passes.
  "u6-no-csrf": {
    file: "lib/csrf.ts",
    from: "  if (SAFE_METHODS.has(request.method)) return null;",
    to: "  return null;\n  if (SAFE_METHODS.has(request.method)) return null;",
    expect: "auth.spec.ts:10a,10b",
  },
  // U7: logout no longer clears the in-memory query cache.
  "u7-no-cache-clear": {
    file: "components/app/UserContext.tsx",
    from: "      queryClient.clear();\n      router.replace(\"/login\");\n      router.refresh();",
    to: "      router.replace(\"/login\");\n      router.refresh();",
    expect: "auth.spec.ts:11",
  },
  // U8: the Usage link is rendered for members too.
  "u8-usage-for-members": {
    file: "components/app/Sidebar.tsx",
    from: "      {isAdmin && (",
    to: "      {true && (",
    expect: "roles.spec.ts:4",
  },
  // U9: the relevance bar is drawn even when scores are not calibrated.
  "u9-score-when-uncalibrated": {
    file: "components/app/chat/SourcesPanel.tsx",
    from: "              {citation.score !== null && <RelevanceBar score={citation.score} />}",
    to: "              <RelevanceBar score={citation.score ?? 0.5} />",
    expect: "app.spec.ts:12",
  },
};

const [, , name, flag] = process.argv;

if (name === "--list") {
  for (const [key, m] of Object.entries(MUTATIONS)) {
    console.log(`${key.padEnd(30)} ${m.file.padEnd(45)} expects ${m.expect}`);
  }
  process.exit(0);
}

const mutation = MUTATIONS[name];
if (!mutation) {
  console.error(`unknown mutation ${name}; known: ${Object.keys(MUTATIONS).join(", ")}`);
  process.exit(2);
}

const revert = flag === "--revert";
const [from, to] = revert ? [mutation.to, mutation.from] : [mutation.from, mutation.to];

const source = readFileSync(mutation.file, "utf8");
// Normalise line endings for matching; the repo is CRLF on Windows and
// a pattern written with \n would silently never match.
const needle = from.replace(/\n/g, source.includes("\r\n") ? "\r\n" : "\n");
const replacement = to.replace(/\n/g, source.includes("\r\n") ? "\r\n" : "\n");

if (!source.includes(needle)) {
  console.error(`TARGET NOT FOUND in ${mutation.file}: ${from.slice(0, 70)}`);
  process.exit(1);
}

writeFileSync(mutation.file, source.replace(needle, replacement));
console.log(`${revert ? "reverted" : "applied"} ${name}`);
