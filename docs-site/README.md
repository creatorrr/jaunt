# Jaunt docs-site

The source for [jaunt.ing](https://jaunt.ing), the documentation site for the
Jaunt spec-driven codegen framework. Built with [Fumadocs](https://fumadocs.dev)
on Next.js, static-exported to GitHub Pages. Pages live in `content/docs/`
(MDX) and follow a Diátaxis-lite layout: `tutorials/`, `guides/`, `concepts/`,
`reference/`.

## Develop

```bash
npm install
npm run dev      # http://localhost:3000
```

## Build

```bash
npm run build    # static export to out/
```

`out/` is gitignored; the build must pass before you commit content changes.

## Deploy

Merging to `main` triggers `.github/workflows/docs-pages.yml`, which runs the
static export and publishes to GitHub Pages. No manual deploy step.
