import { useState, type CSSProperties } from 'react';

import {
  formatLeaderboardScore,
  leaderboardColumns,
  leaderboardEntries,
  leaderboardProviderColors,
  leaderboardScales,
  leaderboardScore,
  rankLeaderboard,
  type ScoreColumn,
} from '@/lib/leaderboard';

import styles from './leaderboard-chart.module.css';
import { LeaderboardModelName, LeaderboardProviderIcon } from './leaderboard-model-name';

type Scale = (typeof leaderboardScales)[number]['id'];
type Entry = (typeof leaderboardEntries)[number];

const linePatterns = [undefined, '7 4', '2 4', '9 3 2 3'] as const;
const overallRanking = rankLeaderboard('overall');
const maxSeries = 3;
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

function ScaleChart({
  activeScale,
  entries,
  onScaleChange,
}: {
  activeScale: Scale;
  entries: readonly Entry[];
  onScaleChange: (scale: Scale) => void;
}) {
  const scores = entries.map((entry) => {
    const siblings = leaderboardEntries.filter(({ provider }) => provider === entry.provider);
    const variant = siblings.indexOf(entry) % linePatterns.length;
    return {
      entry,
      color: leaderboardProviderColors[entry.provider],
      dash: linePatterns[variant],
      points: leaderboardScales.map(({ id }, index) => ({
        id,
        x: (index + 0.5) * 100 / leaderboardScales.length,
        y: Number(((1 - leaderboardScore(entry, id)) * 100).toFixed(4)),
      })),
    };
  });

  return (
    <>
      <ul className={styles.readout} role="list" aria-label="Scores at the selected scale">
        {scores.map(({ entry, color, dash }) => (
          <li key={entry.id}>
            <svg className={styles.swatch} width="28" height="10" viewBox="0 0 28 10" aria-hidden="true">
              <path d="M0 5h28" stroke={color} strokeWidth="2.5" strokeDasharray={dash} />
            </svg>
            <span><LeaderboardModelName model={entry.model} provider={entry.provider} /> <small>· {entry.harness}</small></span>
            <strong>{formatLeaderboardScore(leaderboardScore(entry, activeScale))}</strong>
          </li>
        ))}
      </ul>
      <div className={styles.trendBody}>
        <div className={styles.plot}>
          <Axis />
          <svg className={styles.lines} aria-hidden="true" viewBox="0 0 100 100" preserveAspectRatio="none">
            {scores.map(({ entry, color, dash, points }) => (
              <polyline key={entry.id} points={points.map(({ x, y }) => `${x},${y}`).join(' ')}
                fill="none" stroke={color} strokeWidth="2.5" strokeDasharray={dash}
                strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
            ))}
          </svg>
          {scores.flatMap(({ entry, color, points }) => points.map(({ id, x, y }) => (
            <span key={`${entry.id}-${id}`} className={styles.point} data-active={id === activeScale}
              style={{ left: `${x}%`, top: `${y}%`, backgroundColor: color }} aria-hidden="true" />
          )))}
          <div className={styles.scaleTargets} role="group" aria-label="Inspect scores by scale">
            {leaderboardScales.map((scale) => {
              const scoreList = scores.map(({ entry }) =>
                `${entry.model}, ${entry.harness}, ${formatLeaderboardScore(leaderboardScore(entry, scale.id))}`,
              ).join('; ');
              return (
                <button key={scale.id} type="button" aria-pressed={scale.id === activeScale}
                  aria-label={`${scale.label}: ${scoreList}`}
                  onPointerEnter={(event) => { if (event.pointerType === 'mouse') onScaleChange(scale.id); }}
                  onFocus={() => onScaleChange(scale.id)}
                  onClick={() => onScaleChange(scale.id)}>
                  <span>{scaleLabel(scale)}</span>
                </button>
              );
            })}
          </div>
        </div>
      </div>
    </>
  );
}

export function LeaderboardChart({ onRankChange }: { onRankChange: (column: ScoreColumn) => void }) {
  const [view, setView] = useState<'ranking' | 'trend'>('ranking');
  const [column, setColumn] = useState<ScoreColumn>('overall');
  const [activeScale, setActiveScale] = useState<Scale>('medium');
  const [selected, setSelected] = useState(() => overallRanking.slice(0, maxSeries).map(({ id }) => id));

  return (
    <div className={styles.chart}>
      <div className={styles.views} role="group" aria-label="Chart view">
        <button type="button" aria-pressed={view === 'ranking'} onClick={() => setView('ranking')}>Ranking</button>
        <button type="button" aria-pressed={view === 'trend'} onClick={() => setView('trend')}>Score by Scale</button>
      </div>
      {view === 'ranking' ? (
        <>
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
        </>
      ) : (
        <>
          <details className={styles.modelPicker}>
            <summary>Compare models <span>{selected.length}/{maxSeries}</span></summary>
            <fieldset>
              <legend>Select 1–3 model / harness combinations</legend>
              {overallRanking.map((entry) => {
                const checked = selected.includes(entry.id);
                return (
                  <label key={entry.id}>
                    <input type="checkbox" checked={checked}
                      disabled={checked ? selected.length === 1 : selected.length === maxSeries}
                      onChange={() => setSelected((previous) => previous.includes(entry.id)
                        ? previous.filter((id) => id !== entry.id)
                        : [...previous, entry.id])} />
                    {entry.model} · {entry.harness}
                  </label>
                );
              })}
            </fieldset>
          </details>
          <p className={styles.metric}>
            Diagnosis score <span>· {leaderboardScales.find(({ id }) => id === activeScale)?.label}</span>
          </p>
          <ScaleChart activeScale={activeScale} onScaleChange={setActiveScale}
            entries={overallRanking.filter(({ id }) => selected.includes(id))} />
        </>
      )}
    </div>
  );
}
