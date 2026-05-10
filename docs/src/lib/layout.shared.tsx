import type { BaseLayoutProps } from 'fumadocs-ui/layouts/shared';

export function baseOptions(): BaseLayoutProps {
  return {
    nav: {
      title: 'NetOpsBench',
    },
    links: [
      {
        text: 'Docs',
        url: '/docs',
        active: 'nested-url',
      },
      {
        text: 'Benchmark Results',
        url: '/docs/benchmark/results',
        active: 'nested-url',
      },
    ],
    githubUrl: 'https://github.com/NetX-lab/NetOpsBench',
  };
}
