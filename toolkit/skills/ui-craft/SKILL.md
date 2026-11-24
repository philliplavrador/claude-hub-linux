---
name: ui-craft
description: Small craft rules that make a UI look designed rather than generated: nested radii, alpha borders, OKLCH colour, one complementary accent, fonts that don't read as AI-made, motion timing and the UX laws. Use whenever building, restyling or reviewing any screen, page, component or animation, in any project.
---

**Project notes:** if `.claude/notes/ui-craft.md` exists in this project, read it first; it wins.

# UI craft

Source: design reels watched and transcribed; the one skill every Claude loads for UI work.

## Precedence

A project's own design doc and WCAG 2.1 AA both win over this skill. Where a rule here conflicts with one of them, follow theirs and name
the conflict in your report. Never swap a project's chosen typeface or palette on this skill's say alone: flag
it and ask.

## Surfaces and colour

1. **Nested corners:** inner radius = outer radius minus the padding between them (card 16px with 8px
   padding holds a 8px-radius child). Never the same radius inside and out.
2. **Borders in alpha:** `rgb(0 0 0 / 0.12)` (light) or `rgb(255 255 255 / 0.12)` (dark), not a solid hex,
   so the border blends with whatever is behind it.
3. **Palettes in OKLCH**, not HSL: equal L looks equally bright. Build ramps by stepping L at fixed C and H.
4. **Gradients `in oklch`:** `linear-gradient(90deg in oklch, blue, orange)` keeps the middle vivid instead
   of grey.
5. **Dark mode depth** comes from lighter surfaces as elements rise (background < card < popover), not
   shadows, which barely show on dark.
6. **One accent:** two base colours that get along, plus one accent from the opposite side of the colour
   wheel (navy + glacier blue, add lemon; teal + cream, add orange; cream + ink, add orchid pink). The accent
   goes only on what you are selling: the primary action, the product, the price. Spread it around and it
   stops pointing.
7. **Product shots on white:** `mix-blend-mode: multiply` drops the white box into the page background.

## Detail

8. **Icon stroke matches the text weight beside it** (thin icon next to bold text looks sloppy).
9. **Carousel edges fade:** `mask-image: linear-gradient(90deg, transparent, black 15% 85%, transparent)`.
10. **Icon swaps blur the outgoing icon** while it fades (`filter: blur(4px)` to 0 opacity), e.g. Copy to
    Copied.
11. **Full-height sections use `100dvh`** (`h-dvh`, `min-h-dvh` in Tailwind), never `100vh`/`h-screen`, which
    breaks under Safari's moving toolbar.

## Type

12. Typefaces that read as AI-made: Inter, Roboto, Arial, Geist, Space Grotesk, Bricolage Grotesque,
    Instrument Sans, Instrument Serif, Fraunces, Playfair Display, JetBrains Mono, Space Mono. Avoid them in
    new work; in an existing project, flag rather than replace.
13. Alternatives by feel (many are paid foundry fonts: check the licence before shipping; free ones marked *):
    - Clean Swiss: ABC Diatype, TWK Lausanne, Switzer*, Saans, Areal, Strichpunkt Sans
    - Friendly: Studio Feixen Sans, PP Mori, Aeonik, Degular, GT Walsheim, Circular
    - Personality: Apfel Grotezk, Aktiv Grotesk, ABC Favorit, Familjen Grotesk*, ABC Monument Grotesk
    - Techy mono: ABC Diatype Mono, Söhne Mono, GT America Mono, IBM Plex Mono*, Commit Mono*, Departure Mono*
    - Modern serif: Tiempos Fine, Reckless, Signifier, Canela, Domaine Display
    - Soft serif: EB Garamond*, GT Alpina, Tiempos Text, Lyon Text
    - Vintage: Editorial New, ABC Arizona, GT Super, Blazeface

## Motion (each under `prefers-reduced-motion: reduce` falls back to an instant change or a fade)

14. **Snappy timing:** UI transitions under 300 ms; feedback starts at once.
15. **Ease, never linear:** slow in and slow out; movement follows an arc, not a ruler.
16. **Anticipation and follow-through:** a tiny wind-up before a big move; trailing parts settle after the
    main body stops.
17. **Squash and stretch** on landing and launch gives weight (a few percent, not cartoon).
18. **Staging:** one moving thing leads the eye at a time; a secondary action only reinforces it.
19. **Exaggerated feedback:** push past realistic so it registers (the wrong-password shake).
20. **Pose to pose:** define the key states and let CSS or the motion runtime interpolate.

## UX laws

21. **Fitts:** frequent targets big and close. **Hit areas** can extend past the visible element (keep the
    44x44 minimum).
22. **Hick:** fewer choices, faster decisions. **Miller:** chunk into groups of about 7.
23. **Doherty:** answer within 400 ms or show progress; optimistic UI where it is safe.
24. **Postel:** forgiving with input (trim, accept formats), tidy with output.
25. **Jakob:** people spend most of their time in other apps; match their conventions.
26. **Frequency of use:** the more often an action is used, the less it should move or animate.

## Review checklist

Before calling a screen done, scan it for: same-radius nesting, solid hex borders, HSL ramps or grey-middle
gradients, more than one accent or the accent off the main action, `100vh`, mismatched icon strokes, motion
over 300 ms or linear, hit areas under 44x44, and an AI-tell typeface in new work.
