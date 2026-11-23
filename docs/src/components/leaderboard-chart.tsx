import { useState, type CSSProperties } from 'react';

import {
  formatLeaderboardScore,
  leaderboardColumns,
  leaderboardProviderColors,
  leaderboardScore,
  rankLeaderboard,
  type ScoreColumn,
} from '@/lib/leaderboard';

import styles from './leaderboard-chart.module.css';
import { LeaderboardProviderIcon } from './leaderboard-model-name';

const overallRanking = rankLeaderboard('overall');
const ticks = [100, 75, 50, 25, 0];

function scaleLabel(scale: (typeof leaderboardColumns)[number]) {
  return scale.label.replace('Fat-tree ', '');
}

function Axis() {
  return (
    <div className={styles.axis} aria-hidden="true">
      {ticks.map((tick) => (
        <div className={styles.gridline} key={tick} style={{ top: `${100 - tick}%` }}>
          <span>{tick}%</span>
        </div>
      ))}
    </div>
  );
}

function RankingChart({ column }: { column: ScoreColumn }) {
  const entries = column === 'overall' ? overallRanking : rankLeaderboard(column);

  return (
    <div className={styles.rankingScroll} role="region" tabIndex={0} aria-label="Diagnosis score ranking chart">
      <div className={styles.rankingBody} style={{ '--bar-count': entries.length } as CSSProperties}>
        <div className={styles.plot}>
          <Axis />
          <ol className={styles.bars} role="list">
            {entries.map((entry) => {
              const score = formatLeaderboardScore(leaderboardScore(entry, column));
              const color = leaderboardProviderColors[entry.provider];
              return (
                <li className={styles.bar} key={entry.id}>
                  <div className={styles.barFill} style={{ height: score, backgroundColor: color }}>
                    <span className={styles.barScore}>{score}</span>
                  </div>
                  <div className={styles.barLabel}>
                    <strong>{entry.model}</strong>
                    <span>{entry.harness}</span>
                    <LeaderboardProviderIcon provider={entry.provider} />
                  </div>
                </li>
              );
            })}
          </ol>
        </div>
      </div>
    </div>
  );
}

export function LeaderboardChart({ onRankChange }: { onRankChange: (column: ScoreColumn) => void }) {
  const [column, setColumn] = useState<ScoreColumn>('overall');

  return (
    <div className={styles.chart}>
      <div className={styles.filters} role="group" aria-label="Ranking scale">
        {leaderboardColumns.map((scale) => (
          <button key={scale.id} type="button" aria-pressed={column === scale.id}
            onClick={() => {
              setColumn(scale.id);
              onRankChange(scale.id);
            }}>
            {scaleLabel(scale)}
          </button>
        ))}
      </div>
      <p className={styles.metric}>Diagnosis score <span>· {leaderboardColumns.find(({ id }) => id === column)?.cases} cases</span></p>
      <p className={styles.scrollHint}>Scroll across the chart to see all models.</p>
      <RankingChart column={column} />
    </div>
  );
}
