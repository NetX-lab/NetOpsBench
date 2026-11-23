import Image from 'next/image';

import { withBasePath } from '@/lib/base-path';
import type { leaderboardEntries } from '@/lib/leaderboard';

import styles from './leaderboard.module.css';

type Model = Pick<(typeof leaderboardEntries)[number], 'model' | 'provider'>;

export function LeaderboardProviderIcon({ provider }: Pick<Model, 'provider'>) {
  return (
    <Image
      src={withBasePath(`/assets/leaderboard/${provider}.svg`)}
      alt=""
      width={16}
      height={16}
    />
  );
}

export function LeaderboardModelName({ model, provider }: Model) {
  return (
    <span className={styles.modelName}>
      <LeaderboardProviderIcon provider={provider} />
      <span>{model}</span>
    </span>
  );
}
