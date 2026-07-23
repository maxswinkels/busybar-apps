# BUSY Bar Apps

An unofficial community gallery for apps built for the [BUSY Bar](https://getbusy.bar) — a 72×16 RGB LED matrix productivity gadget.

**Live site:** https://maxswinkels.github.io/busybar-apps/

## About

BUSY Bar Apps is a community-driven repository where developers can share custom applications for their BUSY Bar devices. Apps are submitted via pull request, and the site auto-validates them against a schema before merging.

## How to submit your app

See [CONTRIBUTING.md](./CONTRIBUTING.md) for step-by-step instructions.

**Quick summary:**
1. Fork this repository
2. Create a new folder in `apps/` with your app's slug (kebab-case)
3. Add `app.py`, `manifest.yaml`, and a preview image (`preview.png` or `preview.gif`)
4. Open a pull request

## Local development

```bash
npm install
npm run dev       # Start dev server on localhost:4321
npm run build     # Build for production
```

The site uses [Astro](https://astro.build) for static generation and content collections for schema validation.

## Disclaimer

**BUSY Bar Apps is an unofficial community project.** BUSY Bar is a trademark of Flipper Devices Inc. and this project is not affiliated with or endorsed by Flipper Devices.

## License

MIT License — see [LICENSE](./LICENSE)
