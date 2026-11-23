import type { leaderboardScales } from './leaderboard';

type Scale = (typeof leaderboardScales)[number]['id'];
type Usage = readonly [campaignCases: number, recordedCases: number, inputTokens: number, outputTokens: number];
type UsageByScale = Partial<Record<Scale, Usage>>;

// One selected diagnosis per campaign case. Unavailable usage contributes to campaign cases only.
const usageByRun: Record<string, UsageByScale> = {
  'codex-qwen4-max': { medium: [26, 26, 5006202, 163735], large: [29, 26, 4577554, 140071], xlarge: [30, 22, 4611621, 136147], k8: [60, 51, 12638771, 424968], k12: [59, 48, 13208469, 500011] },
  'codex-kimi-k3-1-pro': { medium: [26, 26, 3675848, 119447], large: [29, 28, 4560873, 130396], xlarge: [30, 24, 5136207, 119332], k8: [60, 57, 13434745, 365639], k12: [59, 54, 13785625, 343155] },
  'codex-gpt-terra': { medium: [26, 25, 5127140, 37612], large: [29, 28, 6188686, 41095], xlarge: [30, 24, 4371226, 36277], k8: [60, 59, 12402264, 94537], k12: [59, 56, 14487437, 99582] },
  'codex-gpt-astra': { medium: [7, 7, 1748937, 10197], large: [8, 8, 1714703, 11020], k8: [35, 33, 9672860, 67188], k12: [38, 35, 10815261, 73588], xlarge: [17, 15, 5148375, 30025] },
  'codex-deepseek-flash': { medium: [26, 26, 5611618, 294765], large: [29, 29, 7701687, 307375], xlarge: [30, 25, 8270565, 323995], k8: [60, 59, 20766618, 1067323], k12: [59, 56, 21178721, 1123442] },
  'codex-gpt': { medium: [64, 64, 12557365, 199749], large: [64, 64, 17379041, 266517], xlarge: [64, 62, 17017413, 211494], k8: [108, 107, 31816189, 433866], k12: [108, 102, 31007470, 392973] },
  'codex-glm': { medium: [26, 26, 3939358, 100315], large: [29, 29, 5014409, 103237], k8: [36, 34, 8410372, 196824], k12: [59, 56, 13145043, 280056] },
  'react-deepseek-pro': { medium: [64, 64, 13630618, 199203], large: [64, 64, 16246682, 199461], xlarge: [64, 60, 20548008, 192248], k8: [108, 107, 46819399, 463853], k12: [108, 105, 43062637, 385350] },
};

const openAi = 'https://developers.openai.com/api/docs/pricing';
const deepSeek = 'https://api-docs.deepseek.com/quick_start/pricing/';

type Price = { input: number; output: number; currency: 'USD' | 'CNY'; source: string };

// Uncached text rates per million tokens, checked 2026-11-23.
// OpenAI Standard short-context, DeepSeek peak, Alibaba Cloud Beijing, and Z.ai USD.
const prices: Record<string, Price> = {
  'codex-gpt-astra': { input: 8, output: 40, currency: 'USD', source: openAi },
  'codex-gpt': { input: 2, output: 10, currency: 'USD', source: openAi },
  'codex-qwen4-max': { input: 10, output: 30, currency: 'CNY', source: 'https://help.aliyun.com/en/model-studio/qwen4-max' },
  'codex-kimi-k3-1-pro': { input: 13, output: 68, currency: 'CNY', source: 'https://help.aliyun.com/en/model-studio/kimi-k3-1-pro' },
  'codex-gpt-terra': { input: 1, output: 5, currency: 'USD', source: 'https://developers.openai.com/api/docs/models/gpt-6.1-terra' },
  'codex-deepseek-flash': { input: 0.3, output: 1.2, currency: 'USD', source: deepSeek },
  'codex-glm': { input: 0.15, output: 0.5, currency: 'USD', source: 'https://docs.z.ai/guides/overview/pricing' },
  'react-deepseek-pro': { input: 1.32, output: 3.96, currency: 'USD', source: deepSeek },
};

// https://www.federalreserve.gov/releases/h10/20261116/
const cnyPerUsd = 6.671;

export function leaderboardCost(id: string) {
  const usage = usageByRun[id] ?? {};
  const price = prices[id];
  let recordedCases = 0;
  let inputTokens = 0;
  let outputTokens = 0;
  for (const [, recorded, input, output] of Object.values(usage)) {
    recordedCases += recorded;
    inputTokens += input;
    outputTokens += output;
  }

  const divisor = price?.currency === 'CNY' ? cnyPerUsd : 1;
  return price && recordedCases > 0
    ? (inputTokens * price.input + outputTokens * price.output) / (1_000_000 * recordedCases * divisor)
    : null;
}
