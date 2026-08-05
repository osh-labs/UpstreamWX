# iOS "Add to Home Screen" walkthrough screenshots

The three slides in the install carousel (`#ios-guide` in `frontend/index.html`,
driven by `initIosGuide()` in `frontend/js/app.js`). Drop the files here with
exactly these names:

| File | Step it shows |
|---|---|
| `ios-1.png` | Safari's toolbar with the **Share** button highlighted |
| `ios-2.png` | The share sheet scrolled to the **Add to Home Screen** row |
| `ios-3.png` | The name/confirm sheet with **Add** highlighted |

## Spec

- **Format** PNG (screenshots have hard UI edges; JPEG artifacts them).
- **Aspect** square (1:1), cropped to the relevant part of the screen. The card
  is laid out for it: it sizes to its content, so the image width binds and the
  caption sits directly beneath with no dead space. Sizing is intrinsic
  (`object-fit: contain`), so a different aspect still renders correctly — it
  just leaves letterboxing, and a tall crop makes the card taller.
- **Width** 800–900 px square. The slide is at most ~390 CSS px wide, so ~800 px
  covers 2× displays with margin; a full device capture is wasted bytes.
- **Consistency** keep all three the same dimensions. The slides share one flex
  row, so the card is as tall as the tallest — a mismatched crop pads the others.
- **Weight** aim for ≤150 KB each. They are lazy: the slides carry `data-src`,
  not `src`, so nothing is fetched until the card is opened for the first time
  — a visitor who never opens it (and every non-iOS visitor) pays zero. They are
  deliberately *not* precached by `sw.js` either, since the card is only useful
  while online. Still, they are committed to the repo, so keep them small.
- **Content** crop to the phone screen, no device bezel. Dark mode matches the
  app; light mode is fine too, the slide background is neutral.

## If a file is missing

`initIosGuide()` hides any `<img>` that fails to load and the slide falls back
to its caption alone, so the card still reads as usable instructions. That is
the current state until these three files land — it is a graceful fallback, not
the intended design.

The captions live in `index.html` next to each `<img>`; edit them there if the
screenshots show a different path than the wording assumes.
