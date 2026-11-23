import { useState } from 'react';

import { leaderboardCost } from '@/lib/leaderboard-cost';
import { formatLeaderboardScore, leaderboardProviderColors, leaderboardScore, rankLeaderboard } from '@/lib/leaderboard';

import { LeaderboardModelName } from './leaderboard-model-name';
import styles from './leaderboard-cost-chart.module.css';

const runs = rankLeaderboard('overall').map((entry) => ({
  ...entry,
  mean: leaderboardCost(entry.id),
  score: leaderboardScore(entry, 'overall'),
}));

const usd = new Intl.NumberFormat('en-US', {
  style: 'currency', currency: 'USD', minimumFractionDigits: 0, maximumFractionDigits: 4,
});
const scoreTicks = [100, 75, 50, 25, 0];
const pricedRuns = runs.flatMap((run) => run.mean !== null && run.mean > 0 ? [{ ...run, mean: run.mean }] : []);
const costs = pricedRuns.map(({ mean }) => mean);
const minCost = costs.length ? Math.min(...costs) : 1;
const maxCost = costs.length ? Math.max(...costs) : 1;
const lowExponent = Math.floor(Math.log10(minCost));
const highExponent = Math.ceil(Math.log10(maxCost));
const allCostTicks = Array.from({ length: highExponent - lowExponent + 1 }, (_, index) =>
  [1, 2, 5].map((factor) => factor * 10 ** (lowExponent + index)),
).flat();
const firstTick = Math.max(0, allCostTicks.findIndex((tick) => tick >= minCost) - 1);
const lastTick = allCostTicks.findIndex((tick) => tick > maxCost);
const costTicks = allCostTicks.slice(firstTick, lastTick + 1);
const low = Math.log10(costTicks[0]);
const span = Math.log10(costTicks.at(-1) ?? 1) - low;
const costX = (cost: number) => 100 * (Math.log10(cost) - low) / span;
const points = pricedRuns.map((run) => ({
  ...run,
  label: pricedRuns.some((other) => other.id !== run.id && other.model === run.model)
    ? `${run.model} · ${run.harness}` : run.model,
  x: costX(run.mean), y: 100 * (1 - run.score),
}));

// Place desktop labels in plot percentages. The point still exposes its full label if space is tight.
type Box = { x: number; y: number; width: number; height: number };
const occupied: Box[] = points.map(({ x, y }) => ({ x: x - 1.2, y: y - 2, width: 2.4, height: 4 }));
const labels = points.flatMap(({ id, label, x, y, provider }) => {
  const width = label.length * 0.91;
  const height = 6;
  const candidates = [
    { x: x + 1.9, y: y - 3 }, { x: x - width - 1.9, y: y - 3, alignRight: true },
    { x: x + 1.9, y: y - 8 }, { x: x - width - 1.9, y: y - 8, alignRight: true },
    { x: x + 1.9, y: y + 2 }, { x: x - width - 1.9, y: y + 2, alignRight: true },
  ];
  const position = candidates.find((candidate) => {
    const right = candidate.x + width;
    const bottom = candidate.y + height;
    return candidate.x >= 0 && right <= 100 && candidate.y >= 0 && bottom <= 100
      && occupied.every((box) => right <= box.x || candidate.x >= box.x + box.width
        || bottom <= box.y || candidate.y >= box.y + box.height);
  });
  if (!position) return [];
  occupied.push({ ...position, width, height });
  return [{ id, label, provider, ...position, x: position.x + (position.alignRight ? width : 0) }];
});

export function LeaderboardCostChart() {
  const [activeId, setActiveId] = useState(points[0]?.id);
  const active = points.find(({ id }) => id === activeId);

  return (
    <section className={styles.section} aria-labelledby="leaderboard-cost-title">
      <h3 id="leaderboard-cost-title">Diagnosis Score vs. Cost</h3>
      <p className={styles.note}>
        Costs use public API rates without cache discounts. Scores are from the full leaderboard, and costs use recorded runs.
      </p>
      {points.length > 0 && (
        <>
          <div className={styles.chart}>
            <div className={styles.axisTitle}>Overall diagnosis score (%)</div>
            <div className={styles.plot} role="group" aria-label="Diagnosis score and cost by model and harness">
              {scoreTicks.map((tick) => (
                <div key={tick} className={styles.horizontal} data-baseline={tick === 0}
                  style={{ top: `${100 - tick}%` }} aria-hidden="true">
                  <span>{tick}%</span>
                </div>
              ))}
              {costTicks.map((tick, index) => (
                <div key={tick} className={styles.vertical} data-alternate={index % 2 === 1}
                  style={{ left: `${costX(tick)}%` }} aria-hidden="true">
                  <span>{usd.format(tick)}</span>
                </div>
              ))}
              {labels.map(({ id, label, provider, x, y, alignRight }) => (
                <span key={id} className={styles.label} data-align-right={alignRight} aria-hidden="true"
                  style={{ left: `${x}%`, top: `${y}%`, color: leaderboardProviderColors[provider] }}>
                  {label}
                </span>
              ))}
              {points.map((run) => (
                <button key={run.id} type="button" className={styles.point} aria-pressed={run.id === activeId}
                  aria-label={`${run.model}, ${run.harness}, ${usd.format(run.mean)} per run, ${formatLeaderboardScore(run.score)} overall`}
                  onPointerEnter={(event) => { if (event.pointerType === 'mouse') setActiveId(run.id); }}
                  onFocus={() => setActiveId(run.id)} onClick={() => setActiveId(run.id)}
                  style={{ left: `${run.x}%`, top: `${run.y}%` }}>
                  <span style={{ backgroundColor: leaderboardProviderColors[run.provider] }} />
                </button>
              ))}
            </div>
            <div className={styles.xTitle}>Mean cost per run (US$, log scale)</div>
          </div>
          {active && (
            <p className={styles.readout} aria-label="Selected model cost">
              <strong><LeaderboardModelName model={active.model} provider={active.provider} /> · {active.harness}</strong>
              <span>{usd.format(active.mean)} per run</span>
            </p>
          )}
        </>
      )}
    </section>
  );
}
