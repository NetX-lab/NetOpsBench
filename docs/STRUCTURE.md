# Documentation site

Edit published pages in `content/docs/`. Each section’s `meta.json` sets its sidebar order. Keep images and downloadable data in `public/assets/`.

The Next.js routes live in `src/app/`, shared MDX components in `src/components/`, and source loading and base-path helpers in `src/lib/`.

From the repository root, run `npm --prefix docs run dev` for local editing. To verify the static export under the preview path, run:

```bash
NEXT_PUBLIC_BASE_PATH=/NetOpsBench-NetX npm --prefix docs run build
```
