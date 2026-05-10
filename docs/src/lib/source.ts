import { loader } from 'fumadocs-core/source';
import { docs } from '../../.source';

const generatedSource = docs.toFumadocsSource();
const generatedFiles =
  typeof (generatedSource as { files: unknown }).files === 'function'
    ? await ((generatedSource as unknown) as { files: () => Promise<unknown[]> }).files()
    : generatedSource.files;

export const source = loader({
  baseUrl: '/docs',
  source: {
    files: generatedFiles as never,
  },
});
