export const leaderboardScales = [
  { id: 'medium', label: 'Medium', cases: 64 },
  { id: 'large', label: 'Large', cases: 64 },
  { id: 'xlarge', label: 'XLarge', cases: 64 },
  { id: 'k8', label: 'Fat-tree k=8', cases: 108 },
  { id: 'k12', label: 'Fat-tree k=12', cases: 108 },
] as const;

type Scale = (typeof leaderboardScales)[number]['id'];
type Counts = readonly [full: number, half: number, zero: number, unscored?: number, assumedFull?: number];

type Entry = {
  id: string;
  model: string;
  harness: string;
  paper?: { label: string; url: string };
  source: 'full-run' | 'subset' | 'projection';
  counts: Record<Scale, Counts>;
};

// Five-scale comparison snapshot, November 2026. For a projected scale,
// the outcome counts describe the projection rather than individual executions.
export const leaderboardEntries: readonly Entry[] = [
  {
    id: 'codex-gpt',
    model: 'GPT-6.1-sol',
    harness: 'Codex',
    source: 'full-run',
    counts: { medium: [57, 4, 3], large: [56, 4, 4], xlarge: [47, 4, 9, 4],
      k8: [73, 2, 27, 6], k12: [70, 2, 25, 11] },
  },
  {
    id: 'codex-deepseek-flash',
    model: 'DeepSeek V4.1 Flash',
    harness: 'Codex',
    source: 'subset',
    counts: { medium: [9, 5, 1, 11, 38], large: [18, 1, 2, 8, 35], xlarge: [13, 2, 3, 12, 34],
      k8: [18, 5, 2, 35, 48], k12: [20, 2, 5, 32, 49] },
  },
  {
    id: 'codex-glm',
    model: 'GLM 5.3 Flash',
    harness: 'Codex',
    source: 'projection',
    counts: { medium: [7, 4, 7, 8, 38], large: [9, 6, 8, 6, 35], xlarge: [43, 6, 15],
      k8: [4, 1, 8, 23, 72], k12: [13, 2, 15, 29, 49] },
  },
  {
    id: 'react-deepseek-pro',
    model: 'DeepSeek V4 Pro',
    harness: 'ReAct',
    source: 'full-run',
    counts: { medium: [38, 12, 11, 3], large: [35, 15, 11, 3], xlarge: [34, 15, 8, 7],
      k8: [48, 24, 26, 10], k12: [49, 22, 29, 8] },
  },
  {
    id: 'tsguard-gpt',
    model: 'GPT-6.1-sol',
    harness: 'TSGuard',
    paper: { label: 'FSE 2026', url: 'https://doi.org/10.1145/3797149' },
    source: 'projection',
    counts: { medium: [39, 2, 23], large: [39, 1, 24], xlarge: [34, 4, 26],
      k8: [51, 8, 49], k12: [49, 9, 50] },
  },
];

export const leaderboardCaseCount = leaderboardScales.reduce((sum, scale) => sum + scale.cases, 0);

export function caseTotals([full, half, zero, unscored = 0, assumedFull = 0]: Counts) {
  return { points: full + half / 2 + assumedFull, cases: full + half + zero + unscored + assumedFull };
}

export type ScoreColumn = 'overall' | Scale;

export function leaderboardScore(entry: Entry, column: ScoreColumn): number {
  if (column !== 'overall') {
    const { points, cases } = caseTotals(entry.counts[column]);
    return points / cases;
  }
  const points = leaderboardScales.reduce((sum, { id }) => sum + caseTotals(entry.counts[id]).points, 0);
  return points / leaderboardCaseCount;
}
