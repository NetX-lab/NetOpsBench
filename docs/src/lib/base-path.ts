export const siteBasePath = process.env.NEXT_PUBLIC_BASE_PATH || '';

export function withBasePath(path: string): string {
  if (!siteBasePath) return path;
  if (path.startsWith(siteBasePath)) return path;
  return `${siteBasePath}${path}`;
}