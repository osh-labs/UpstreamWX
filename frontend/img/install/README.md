# iOS "Add to Home Screen" walkthrough screenshots

The three slides in the install carousel (`#ios-guide` in `frontend/index.html`,
driven by `initIosGuide()` in `frontend/js/app.js`).

| File | Step it shows |
|---|---|
| `ios-1.jpg` | Safari's address bar, the **•••** More button circled at its right |
| `ios-2.jpg` | The Safari menu, **Share** highlighted at the top |
| `ios-3.jpg` | The share sheet scrolled to a highlighted **Add to Home Screen** |

Note the flow is ••• → Share → Add to Home Screen, not a Share button sitting
directly in the toolbar. The captions in `index.html` match these images; if you
recapture on an iOS version with a different toolbar, update both together.

## Spec

- **Format** JPEG. These are mostly iOS's translucent blurred menu surfaces,
  which PNG encodes badly — PNG came out 3–4× larger for identical output.
  Quality 86 with 4:2:2 chroma; artifacts are invisible once downscaled into the
  slide.
- **Aspect** square (1:1), cropped to the relevant part of the screen. The card
  is laid out for it: it sizes to its content, so the image width binds and the
  caption sits directly beneath with no dead space. Sizing is intrinsic
  (`object-fit: contain`), so a different aspect still renders correctly — it
  just leaves letterboxing, and a tall crop makes the card taller.
- **Size** 1170 px square, ~70–90 KB each. The slide is at most 388 CSS px wide,
  so 1170 is exactly 3× — pixel-perfect on a Pro-class iPhone with nothing spare.
- **Consistency** keep all three the same dimensions. The slides share one flex
  row, so the card is as tall as the tallest — a mismatched crop pads the others.
- **Annotation** the red circle/box marking the tap target is part of the image,
  drawn before committing. The app adds no overlay of its own.

## Loading

The slides carry `data-src`, not `src`, so nothing is fetched until the card is
opened for the first time — a visitor who never opens it (and every non-iOS
visitor) pays zero. They are deliberately *not* precached by `sw.js` either,
since the card is only useful while online.

If a file is missing, `initIosGuide()` hides that `<img>` and the slide falls
back to its caption alone, so the card still reads as usable instructions.
