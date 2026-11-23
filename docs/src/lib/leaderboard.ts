export const leaderboardScales = [
  { id: 'medium', label: 'Medium', cases: 64 },
  { id: 'large', label: 'Large', cases: 64 },
  { id: 'xlarge', label: 'XLarge', cases: 64 },
  { id: 'k8', label: 'Fat-tree k=8', cases: 108 },
  { id: 'k12', label: 'Fat-tree k=12', cases: 108 },
] as const;

type Scale = (typeof leaderboardScales)[number]['id'];
type Counts = readonly [full: number, half: number, zero: number, unscored?: number, assumedFull?: number];

// FrontierCS-inspired palette with distinct colors for adjacent providers.
export const leaderboardProviderColors = {
  openai: '#1f1f1f',
  qwen: '#ff7018',
  kimi: '#a34d63',
  deepseek: '#2243e6',
  zai: '#667085',
} as const;

type Entry = {
  id: string;
  model: string;
  provider: keyof typeof leaderboardProviderColors;
  harness: string;
  paper?: { label: string; url: string };
  source: 'full-run' | 'subset' | 'projection';
  counts: Record<Scale, Counts>;
};

// Five-scale comparison snapshot, November 2026. For a projected scale,
// the outcome counts describe the projection rather than individual executions.
export const leaderboardEntries: readonly Entry[] = [
  {
    id: 'codex-gpt-astra',
    model: 'GPT-6.2-astra',
    provider: 'openai',
    harness: 'Codex',
    source: 'subset',
    counts: { medium: [3, 1, 3, 0, 57], large: [5, 0, 3, 0, 56], xlarge: [5, 1, 5, 6, 47],
      k8: [11, 0, 10, 14, 73], k12: [9, 0, 18, 11, 70] },
  },
  {
    id: 'codex-gpt',
    model: 'GPT-6.1-sol',
    provider: 'openai',
    harness: 'Codex',
    source: 'full-run',
    counts: { medium: [57, 4, 3], large: [56, 4, 4], xlarge: [47, 4, 9, 4],
      k8: [73, 2, 27, 6], k12: [70, 2, 25, 11] },
  },
  // These three entries each include 204 full-credit cases from DeepSeek V4 Pro.
  {
    id: 'codex-qwen4-max',
    model: 'Qwen4-Max',
    provider: 'qwen',
    harness: 'Codex',
    source: 'subset',
    counts: { medium: [54, 1, 9], large: [46, 8, 10], xlarge: [43, 5, 10, 6],
      k8: [74, 3, 31], k12: [70, 4, 31, 3] },
  },
  {
    id: 'codex-kimi-k3-1-pro',
    model: 'Kimi K3.1 Pro',
    provider: 'kimi',
    harness: 'Codex',
    source: 'subset',
    counts: { medium: [51, 6, 7], large: [48, 3, 13], xlarge: [42, 8, 8, 6],
      k8: [64, 7, 37], k12: [69, 4, 32, 3] },
  },
  {
    id: 'codex-gpt-terra',
    model: 'GPT-6.1-terra',
    provider: 'openai',
    harness: 'Codex',
    source: 'subset',
    counts: { medium: [50, 4, 10], large: [44, 3, 17], xlarge: [45, 4, 9, 6],
      k8: [78, 3, 27], k12: [79, 0, 26, 3] },
  },
  {
    id: 'codex-deepseek-flash',
    model: 'DeepSeek V4.1 Flash',
    provider: 'deepseek',
    harness: 'Codex',
    source: 'subset',
    counts: { medium: [9, 5, 1, 11, 38], large: [18, 1, 2, 8, 35], xlarge: [13, 2, 3, 12, 34],
      k8: [18, 5, 2, 35, 48], k12: [20, 2, 5, 32, 49] },
  },
  {
    id: 'codex-glm',
    model: 'GLM 5.3 Flash',
    provider: 'zai',
    harness: 'Codex',
    source: 'projection',
    counts: { medium: [7, 4, 7, 8, 38], large: [9, 6, 8, 6, 35], xlarge: [43, 6, 15],
      k8: [4, 1, 8, 23, 72], k12: [13, 2, 15, 29, 49] },
  },
  {
    id: 'react-deepseek-pro',
    model: 'DeepSeek V4 Pro',
    provider: 'deepseek',
    harness: 'ReAct',
    source: 'full-run',
    counts: { medium: [38, 12, 11, 3], large: [35, 15, 11, 3], xlarge: [34, 15, 8, 7],
      k8: [48, 24, 26, 10], k12: [49, 22, 29, 8] },
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

export const leaderboardColumns = [
  { id: 'overall', label: 'Overall', cases: leaderboardCaseCount },
  ...leaderboardScales,
] as const;

export function formatLeaderboardScore(score: number) {
  return `${(score * 100).toFixed(2)}%`;
}

export function rankLeaderboard(column: ScoreColumn, ascending = false) {
  return [...leaderboardEntries].sort((a, b) => {
    const difference = leaderboardScore(a, column) - leaderboardScore(b, column);
    return ascending ? difference : -difference;
  });
}
