'use client';

import { useState } from 'react';
import { ArrowDown, ArrowDownUp, ArrowUp } from 'lucide-react';

import {
  leaderboardCaseCount,
  formatLeaderboardScore,
  leaderboardColumns,
  leaderboardProviderColors,
  leaderboardScales,
  leaderboardScore,
  rankLeaderboard,
  type ScoreColumn,
} from '@/lib/leaderboard';

import styles from './leaderboard.module.css';
import { LeaderboardChart } from './leaderboard-chart';
import { LeaderboardCostChart } from './leaderboard-cost-chart';
import { LeaderboardModelName } from './leaderboard-model-name';

export function Leaderboard() {
  const [sort, setSort] = useState<{ column: ScoreColumn; ascending: boolean }>({
    column: 'overall',
    ascending: false,
  });
  const entries = rankLeaderboard(sort.column, sort.ascending);

  return (
    <section className={styles.section} id="leaderboard" aria-labelledby="leaderboard-title">
      <div className={styles.inner}>
        <h2 id="leaderboard-title">Leaderboard</h2>
        <p className={styles.intro}>
          Diagnosis scores across {leaderboardCaseCount} cases and {leaderboardScales.length} network scales.
        </p>
        <LeaderboardChart onRankChange={(column) => setSort({ column, ascending: false })} />
        <p className={styles.scrollHint}>Scroll across the table to see all five scales.</p>
        <div className={styles.tableScroll} role="region" tabIndex={0} aria-label="Leaderboard scores">
          <table className={styles.table}>
            <thead>
              <tr>
                <th scope="col" className={styles.identity}>Model / Harness</th>
                {leaderboardColumns.map(({ id, label, cases }) => {
                  const selected = sort.column === id;
                  const SortIcon = selected ? (sort.ascending ? ArrowUp : ArrowDown) : ArrowDownUp;
                  return (
                    <th key={id} scope="col" aria-sort={selected ? (sort.ascending ? 'ascending' : 'descending') : undefined}>
                      <button
                        type="button"
                        className={styles.sort}
                        onClick={() => setSort({ column: id, ascending: selected && !sort.ascending })}
                      >
                        <span>{label}<SortIcon size={13} aria-hidden="true" /></span>
                        <small>{cases} cases</small>
                      </button>
                    </th>
                  );
                })}
              </tr>
            </thead>
            <tbody>
              {entries.map((entry) => (
                <tr key={entry.id}>
                  <th scope="row" className={styles.identity}>
                    <span className={styles.model}>
                      <LeaderboardModelName model={entry.model} provider={entry.provider} />
                    </span>
                    <span className={styles.harness}>
                      {entry.harness}
                      {entry.paper && (
                        <a className={styles.citation} href={entry.paper.url} aria-label={`${entry.harness} paper, ${entry.paper.label}`}>
                          {entry.paper.label}
                        </a>
                      )}
                    </span>
                  </th>
                  {leaderboardColumns.map(({ id }) => {
                    const score = leaderboardScore(entry, id);
                    const percent = formatLeaderboardScore(score);
                    return id === 'overall' ? (
                      <td key={id} className={styles.overall}>
                        <strong>{percent}</strong>
                        <span className={styles.track} aria-hidden="true">
                          <span className={styles.fill}
                            style={{ width: percent, backgroundColor: leaderboardProviderColors[entry.provider] }} />
                        </span>
                      </td>
                    ) : <td key={id}>{percent}</td>;
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <LeaderboardCostChart />
      </div>
    </section>
  );
}
