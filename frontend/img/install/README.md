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
- **Aspect** portrait phone, roughly 9:19.5. The slide scales the image with
  `object-fit: contain`, so any portrait aspect renders correctly — it just
  leaves more letterboxing the further it strays.
- **Width** 750–900 px is plenty. The slide is at most ~400 CSS px wide, so a
  full 1179 px device capture is wasted bytes; downscale before committing.
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
