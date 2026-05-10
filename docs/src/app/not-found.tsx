import Link from 'next/link';

export default function NotFound() {
  return (
    <main className="mx-auto flex min-h-screen w-full max-w-3xl flex-col justify-center gap-6 px-6 py-16">
      <div className="flex flex-col gap-3">
        <p className="text-sm font-semibold uppercase text-fd-muted-foreground">404</p>
        <h1 className="text-4xl font-bold text-fd-foreground">Page not found</h1>
        <p className="text-base leading-7 text-fd-muted-foreground">
          The page you requested does not exist in the NetOpsBench documentation.
        </p>
      </div>
      <div>
        <Link className="font-medium text-fd-primary underline underline-offset-4" href="/docs">
          Go to documentation
        </Link>
      </div>
    </main>
  );
}
