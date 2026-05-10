# Docs Structure

This documentation site follows a Harbor-Lite layout:

- `src/app/` - Next.js App Router pages, layouts, and route-local styles
- `src/components/` - MDX component bindings and shared UI components
- `src/lib/` - shared site logic such as source loading, layout options, and base-path helpers
- `content/docs/` - the single source of truth for published documentation content
- `public/assets/` - static assets served by the site at runtime
- `_legacy/` - archived historical markdown files kept for reference only

## Rules

1. Add new documentation pages under `content/docs/`.
2. Add new routes, layouts, and page-local CSS under `src/app/`.
3. Add shared rendering helpers and source plumbing under `src/lib/`.
4. Add reusable MDX/UI components under `src/components/`.
5. Do not add new business documentation markdown files to the `docs/` root.
6. Prefer `@/` imports for code under `src/` instead of deep relative paths.

## Asset guidance

- Use `public/assets/` for images that must be publicly addressable from pages or MDX.
- Treat `assets/` as temporary or source-material storage only until it is fully audited.
- If an asset is referenced via `/assets/...`, it should exist under `public/assets/`.

## Validation

After structural changes, run:

```bash
cd docs
npm run build
npm run dev
```
