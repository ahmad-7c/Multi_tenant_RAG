---
name: "documind-ui-design"
description: "Visual design system for the DocuMind UI: warm black and dark orange, animated, modern. Tokens, components, motion rules, per-screen layout specs, and a fidelity process with a builder and reviewer."
---
 
# DocuMind UI Design System
 
This skill defines WHAT the DocuMind UI looks and moves like. The skill `documind-ui-phase` defines HOW it is built (BFF, cookie auth, security, tests, checkpoints). Use both together. If they conflict: security and architecture follow documind-ui-phase; visuals follow this skill.
 
Goal: a UI that looks like a premium developer product. Dark, warm black surfaces, one deep orange accent, purposeful animation. The human approved a visual prototype with these exact tokens and layouts; match it closely, do not reinterpret it.
 
## Operating rules
 
1. Tokens first. Build tokens, fonts, and base components before any screen. Never hard-code a colour, radius, or duration in a screen; use tokens.
2. One accent. Orange is the only brand colour. Amber is a secondary highlight used only inside gradients and glows. Green, red, and neutral are for status only.
3. Restraint. At most ONE focal animated element per screen (animated conic border, glow orb, or ring). Everything else uses subtle transitions.
4. Every animation must respect `prefers-reduced-motion` and must never block interaction.
5. Dark theme only for now. Structure tokens as CSS variables on `:root` so a light theme can be added later via `data-theme`, but do not build it.
6. Do not invent features or data. Anything not backed by a real endpoint is shown only if the backend provides it; otherwise omit it or show an honest placeholder, and report it.
7. Copy: sentence case, verb-first buttons, no exclamation marks, no 'successfully', no 'please'. Errors say what happened and what to do. Refusals are information, not errors.
8. Report what you did NOT verify. Never claim visual fidelity without screenshots.
## Tokens (put in `app/tokens.css` and map into the Tailwind theme)
 
```css
:root{
  --bg:#070605; --s1:#0D0B09; --s2:#141110; --s3:#1C1815;
  --line:rgba(255,255,255,.08); --line2:rgba(255,255,255,.14);
  --tx:#F5EFE8; --tx2:#B8AEA4; --tx3:#938981;
  --ac:#FF6B00; --ac2:#FF8A3D; --amber:#FFB020; --on-ac:#0A0705;
  --ok:#34D399; --ok-tx:#6EE7B7; --err:#F87171; --err-tx:#FCA5A5;
  --ac-tint:rgba(255,107,0,.16); --ac-ring:rgba(255,107,0,.22);
  --r-ctl:10px; --r-card:16px; --r-pill:999px;
  --ease:cubic-bezier(.2,.7,.2,1);
}
```
 
Contrast rules (already checked, keep them): primary button is orange background with near-black text `--on-ac` (about 6.9:1). Never put white text on orange. Orange text on dark uses `--ac2` or lighter (`#FFB27A`, `#FFD2AD`). Secondary text `--tx2`, hints `--tx3`; nothing dimmer than `--tx3`.
 
Typography (load with next/font/google so fonts are self-hosted at build): Sora 500/600 for headings and big numbers, Inter 400/500/600 for UI text, JetBrains Mono for numbers in chips, code, timings, and keyboard hints. Sizes: page title 30px/600, section title 16px/600, body 14-15px, meta 12-13px, chips 12px/500. Letter-spacing -.02em on headings over 24px. Headings and big numbers use Sora; do not use Sora for body text.
 
Shape and spacing: 4px base scale. Controls radius 10px, cards 16px, chips pill. Sidebar 256px, topbar 72px, content padding 40px, card padding 20-22px, gaps 16px. Borders are 1px `--line`, hover `--line2`. No heavy drop shadows; depth comes from surface steps (s1 to s3) and borders. Glow is only on primary-button hover and the one focal element.
 
## Motion system
 
Durations: hover and focus 200-250ms; entrance 600-800ms with `--ease`; stagger 50-100ms, entrance sequence capped at about 1s total; streaming caret blink 1s step.
 
Allowed continuous animations (pick sparingly): status-dot ping, indeterminate or active progress shimmer (background-position), streaming caret, the single focal conic-border or orb. Everything else animates once on mount or on hover.
 
Performance: animate `transform` and `opacity` wherever possible. Exceptions kept deliberately small: progress shimmer (background-position), conic border (`@property --a` angle), `stroke-dashoffset` for sparklines and the ring. Never animate layout properties (width, height, top, left) except the one-time bar grow on mount. Never animate per streamed token; stream text without animation and show only the caret. Use `will-change` sparingly and only for the focal element.
 
Signature effects (implement exactly these, not more):
- Entrance: elements fade up 14-16px (`fadeUp`), staggered by section.
- Primary button hover: lift 2px, orange glow `0 10px 30px rgba(255,107,0,.5)`, a light sweep across the button.
- Focal conic border: 1px border painted with `conic-gradient(from var(--a), transparent, #FF6B00, #FFB020, transparent)`, masked to the border box, `--a` animated 0 to 360deg over 5s linear. Used on the login demo card and the active source card in chat.
- Login hero: two blurred radial orbs (orange, amber) drifting slowly (14-19s), plus a faint 56px grid masked by a radial gradient. Login only.
- Typing demo on the login hero: question types in with `steps()`, trace chips and answer reveal in sequence, citation chips pop with a slight overshoot. Plays once.
- Progress bars: orange to amber gradient with moving shimmer while active; one-time grow on mount.
- Status dot: green dot with an expanding ping ring for live or healthy states.
- Sparklines and the budget ring draw on mount using `stroke-dashoffset` with `pathLength`.
- Chart bars rise from the baseline on mount, staggered.
- Cards lift 2px with a stronger border on hover.
Reduced motion: when `prefers-reduced-motion: reduce`, disable entrance animations, orbs, typing, conic spin, and shimmer; show final states immediately.
 
## Components (build and export these first)
 
Button (primary: orange with dark text; ghost; icon button 34px), Input (46px high, `--s2` background, focus ring `0 0 0 4px var(--ac-ring)` plus `--ac2` border), StatusChip (ready green, processing orange with spinner, queued neutral with clock icon, failed red with alert icon; always icon plus text), CitationChip (20px square, orange tint, mono-ish 11px, hover lifts), SourcePill (citation chip plus 'file.pdf, p.12'), TraceChip (mono 11px, e.g. 'hybrid search 42 ms', 'rerank 118 ms', 'confidence 0.82' in green), Card, NavItem (active: orange tint background with a 3px orange indicator on the sidebar edge), ProgressBar, BudgetMeter, Ring, Sparkline, Segmented control, Avatar (orange to amber gradient with dark initials), EmptyState, InlineBanner (info, error), Toast, Skeleton (dark shimmer).
Build a dev-only route `/design` that renders every component in every state; it is the reference page for fidelity review and must be excluded from production builds.
 
## Screens (layout spec)
 
App shell: left sidebar 256px (logo mark plus wordmark, search button with 'Ctrl K' hint that opens a simple command palette only if built, nav: Documents, Chat, Usage for admins; bottom: daily AI budget meter, org avatar, org name, role, sign-out icon). Top bar 72px with breadcrumb 'Org / Page' and a system status chip with a ping dot. Content column is fluid with 40px padding. On narrow screens the sidebar becomes a top bar with a menu; tables scroll horizontally inside a box.
 
1. Login: two columns. Left hero (fluid): logo, headline 'Ask your documents. Get answers you can verify.' with the last word in an orange to amber gradient, one-line subcopy, the animated demo card with the focal conic border, three trust points (tenant isolation, cited answers, streaming responses). Right panel 520px on `--s1`: 'Welcome back', fields Organization, Email, Password, primary 'Sign in' button with an arrow, 'New to DocuMind? Create an organization' link, small demo note. Generic error text only.
2. Documents: title, summary line, primary 'Upload document' button; dashed orange dropzone with a gently bobbing icon and the pipeline hint (extract, chunk, embed, ready); table card with columns Name, Status, Pages, Uploaded, actions. Processing rows show a status chip and a progress bar with the stage; failed rows show the reason in red below the chip. A footer line with a ping dot says status is checked every 3 seconds while documents are processing.
3. Chat: top bar with conversation title and an 'Org only' scope chip. Messages column (max 760px): user bubbles are orange with dark text, right-aligned; assistant messages have a small logo avatar. Assistant answer shows trace chips first, then the streamed answer with citation chips inline and a blinking caret while streaming, then source pills. Refusal is an info card with an amber tint, title 'I couldn't find this in your documents', and a short next step. Composer is a rounded bar with 'Ask about your documents', and a Stop button (with a soft pulsing ring) while streaming. Right Sources panel (about 340-400px) slides in: each source card shows citation number, file, page, relevance score with a small bar, the parent passage with the matched sentence highlighted, and 'Open in document'. The active source has the focal conic border.
4. Usage (admin only): title plus Segmented range (Today, 7 days, 30 days); four metric cards (Questions, Tokens, Cache hit rate, Refusal rate) each with value in Sora, a delta chip, and a drawn sparkline; a stacked bar chart 'Questions per day' (answered live in orange, from cache in amber) with legend; a Daily budget ring with plain explanation; a 'Where the time goes' card with three bars (hybrid search, rerank, first token). Only show metrics the backend actually returns; omit or mark the rest.
## Data honesty
 
Trace chips, confidence, relevance scores, timings, and budget percentages must come from real backend fields. In Step 0 of documind-ui-phase, list which of these exist. For any that do not, propose the smallest backend change, and meanwhile hide the element rather than faking numbers. Never ship invented statistics.
 
## Responsive and accessibility
 
Test widths 1440, 1024, and 390. No horizontal page scroll at any width. Touch targets at least 44px on mobile. Visible focus rings (orange ring) on every interactive element. Real buttons and links, labelled icon-only buttons, aria-live (polite, throttled) for streaming and status changes, text contrast at least 4.5:1, status never by colour alone.
 
## Fidelity process (build, then independent review)
 
Use the subagents `ui-builder` (writes UI code, one screen or component group per task) and `ui-fidelity-reviewer` (read-only; judges screenshots and code against this spec), with `ui-qa-runner` producing screenshots. The agent that builds never judges its own output.
 
Loop per screen: (1) ui-builder implements from this spec; (2) ui-qa-runner captures screenshots at 1440, 1024, and 390 in the real running app; (3) ui-fidelity-reviewer returns a findings table; (4) fix the findings; (5) stop and show the human the screenshots and the remaining findings; (6) move on only after the human approves.
 
Order: D0 tokens, fonts, base components, `/design` page; D1 app shell plus Login; D2 Documents; D3 Chat; D4 Usage; D5 polish pass (motion audit, reduced-motion check, responsive check).
 
## Definition of done
 
Every token comes from `tokens.css`; no raw hex in screens; the `/design` page shows all components and states; all four screens match this spec at the three widths; reduced-motion works; no layout shift on load; Lighthouse accessibility score at least 95 for each screen; animations stay smooth (no long tasks during streaming); the fidelity reviewer's last report has no high-severity findings; the human approved screenshots for every screen.