import './globals.css';

import { RootProvider } from 'fumadocs-ui/provider/next';
import { Inter, JetBrains_Mono } from 'next/font/google';
import type { Metadata } from 'next';
import type { ReactNode } from 'react';

import { withBasePath } from '@/lib/base-path';

const sans = Inter({
  subsets: ['latin'],
  weight: ['400', '500', '600', '700'],
  variable: '--font-sans',
});

const mono = JetBrains_Mono({
  subsets: ['latin'],
  weight: ['400', '500', '600'],
  variable: '--font-mono',
});

export const metadata: Metadata = {
  title: 'NetOpsBench: Open Arena for NetOps in AI Infrastructure',
  description: 'Fair, reproducible benchmarks for agentic network troubleshooting with realistic environments, tracing and telemetry tooling, and an open SDK.',
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className={`${sans.className} ${sans.variable} ${mono.variable} flex min-h-screen flex-col`}>
        <RootProvider search={{ options: { type: 'static', api: withBasePath('/api/search') } }}>
          {children}
          <footer className="mt-auto border-t border-fd-border bg-fd-background px-6 py-4 text-center text-sm text-fd-muted-foreground">
            © 2026 NetX Lab. Released under the{' '}
            <a
              className="font-medium text-fd-foreground underline underline-offset-4 hover:text-fd-primary"
              href="https://github.com/NetX-lab/NetOpsBench/blob/main/LICENSE"
            >
              MIT License
            </a>
            .
          </footer>
        </RootProvider>
      </body>
    </html>
  );
}
