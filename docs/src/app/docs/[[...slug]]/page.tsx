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

export function generateStaticParams() {
  return source.generateParams();
}

function mapLegacySlug(slug?: string[]): string[] | null {
  if (!slug || slug.length === 0) return null;

  if (slug[0] === 'guides') {
    if (slug[1] === 'getting-started') return ['getting-started'];
    if (slug[1] === 'deployment') return ['operations', 'deployment'];
    if (slug[1] === 'observability') return ['operations', 'observability'];
  }

  if (slug[0] === 'contributor') {
    return ['contribute', ...slug.slice(1)];
  }

  return null;
}

export default async function DocsRoute(props: PageProps) {
  const params = await props.params;
  let page = source.getPage(params.slug);

  if (!page) {
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
