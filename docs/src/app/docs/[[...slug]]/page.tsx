import { notFound, redirect } from 'next/navigation';
import type { ComponentType } from 'react';
import type { TOCItemType } from 'fumadocs-core/toc';
import { DocsBody, DocsDescription, DocsPage, DocsTitle } from 'fumadocs-ui/page';

import { getMDXComponents } from '@/components/mdx';
import { source } from '@/lib/source';

type PageProps = {
  params: Promise<{ slug?: string[] }>;
};

type DocsPageData = {
  body: ComponentType<{ components?: ReturnType<typeof getMDXComponents> }>;
  title: string;
  description?: string;
  toc?: TOCItemType[];
};

const archivedPages: Record<string, string> = {
  'architecture/core-simplification': '/docs/architecture/system-overview',
  'architecture/runtime-audit': '/docs/architecture/system-overview',
  'architecture/recovery-v2-audit': '/docs/architecture/state-checked-recovery',
  'releases/v0.2.0': '/#leaderboard',
  'run-benchmarks/results': '/#leaderboard',
  'benchmark/results': '/#leaderboard',
};

const legacySlugs = [
  ...Object.keys(archivedPages).map((path) => path.split('/')),
  ['getting-started'],
  ['benchmark', 'methodology'],
  ['examples', 'run-scenario-vs-suite'],
  ['examples', 'scale-and-batch-benchmarks'],
  ['examples', 'llm-fault-type-judge'],
  ['examples', 'manual-runtime'],
  ['examples', 'custom-fault-example'],
  ['operations', 'observability'],
  ['operations', 'deployment'],
  ['contribute', 'custom-faults'],
  ['contribute', 'fault-types'],
  ['contribute', 'repository-layout'],
  ['api', 'quickstart'],
  ['api', 'cli'],
  ['api', 'reference'],
  ['guides', 'getting-started'],
  ['guides', 'deployment'],
  ['guides', 'observability'],
  ['contributor', 'repository-layout'],
  ['contributor', 'custom-faults'],
  ['contributor', 'fault-types'],
];

export function generateStaticParams() {
  return [
    ...source.generateParams(),
    ...legacySlugs.map((slug) => ({ slug })),
  ];
}

function mapLegacySlug(slug?: string[]): string[] | null {
  if (!slug || slug.length === 0) return null;

  if (slug[0] === 'getting-started') return ['quickstart'];

  if (slug[0] === 'benchmark') {
    if (slug[1] === 'methodology') return ['run-benchmarks', 'methodology'];
  }

  if (slug[0] === 'examples') {
    if (slug[1] === 'run-scenario-vs-suite') return ['run-benchmarks', 'run-scenario-vs-suite'];
    if (slug[1] === 'scale-and-batch-benchmarks') return ['run-benchmarks', 'scale-and-batch-benchmarks'];
    if (slug[1] === 'llm-fault-type-judge') return ['run-benchmarks', 'methodology'];
    if (slug[1] === 'manual-runtime') return ['debug-operate', 'observability'];
    if (slug[1] === 'custom-fault-example') return ['extend-netopsbench', 'custom-faults'];
  }

  if (slug[0] === 'operations') {
    if (slug[1] === 'observability') return ['debug-operate', 'observability'];
    if (slug[1] === 'deployment') return ['debug-operate', 'host-setup'];
  }

  if (slug[0] === 'contribute') {
    if (slug[1] === 'custom-faults') return ['extend-netopsbench', 'custom-faults'];
    if (slug[1] === 'fault-types') return ['extend-netopsbench', 'custom-faults'];
    if (slug[1] === 'repository-layout') return ['architecture', 'system-overview'];
  }

  if (slug[0] === 'api') {
    if (slug[1] === 'quickstart') return ['build-your-agent', 'python-api-guide'];
    if (slug[1] === 'cli') return ['run-benchmarks', 'run-scenario-vs-suite'];
    if (slug[1] === 'reference') return ['build-your-agent', 'python-api-guide'];
  }

  if (slug[0] === 'guides') {
    if (slug[1] === 'getting-started') return ['quickstart'];
    if (slug[1] === 'deployment') return ['debug-operate', 'host-setup'];
    if (slug[1] === 'observability') return ['debug-operate', 'observability'];
  }

  if (slug[0] === 'contributor') {
    if (slug[1] === 'repository-layout') return ['architecture', 'system-overview'];
    if (slug[1] === 'custom-faults') return ['extend-netopsbench', 'custom-faults'];
    if (slug[1] === 'fault-types') return ['extend-netopsbench', 'custom-faults'];
  }

  return null;
}

export default async function DocsRoute(props: PageProps) {
  const params = await props.params;
  let page = source.getPage(params.slug);

  if (!page) {
    const archived = archivedPages[params.slug?.join('/') ?? ''];
    if (archived) redirect(archived);

    const legacySlug = mapLegacySlug(params.slug);

    if (legacySlug) {
      redirect(`/docs/${legacySlug.join('/')}`);
    }
  }

  if (!page) notFound();

  const data = page.data as DocsPageData;
  const MDX = data.body;

  return (
    <DocsPage
      toc={data.toc}
      tableOfContent={{ style: 'clerk' }}
      editOnGithub={{
        owner: 'NetX-lab',
        repo: 'NetOpsBench',
        sha: 'main',
        path: `docs/content/docs/${page.path}`,
      }}
    >
      <DocsTitle>{data.title}</DocsTitle>
      <DocsDescription>{data.description}</DocsDescription>
      <DocsBody>
        <MDX components={getMDXComponents()} />
      </DocsBody>
    </DocsPage>
  );
}
