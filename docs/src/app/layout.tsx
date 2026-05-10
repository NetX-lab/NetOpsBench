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
  title: 'NetOpsBench',
  description: 'A benchmark for data-center network troubleshooting agents built on SONiC-VS, Containerlab, and rich observability.',
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className={`${sans.className} ${sans.variable} ${mono.variable} flex min-h-screen flex-col`}>
        <RootProvider search={{ options: { type: 'static', api: withBasePath('/api/search') } }}>
          {children}
        </RootProvider>
      </body>
    </html>
  );
}
