import Link from 'next/link';
import {
  Boxes,
  ChartColumnBig,
  Network,
  ShieldCheck,
} from 'lucide-react';

import styles from './page.module.css';
import { withBasePath } from '@/lib/base-path';

const metrics = [
  {
    value: null,
    tag: 'Reproducible',
    label: 'Replayable Faults',
    sub: 'Controlled episodes with scenario ground truth',
  },
  {
    value: null,
    tag: 'Interactive',
    label: 'Live Environment',
    sub: 'Agents operate against runtime state, not static logs',
  },
  {
    value: null,
    tag: 'Signals',
    label: 'Telemetry & Probing',
    sub: 'Pingmesh · BGP · syslog · counters · Grafana',
  },
  {
    value: null,
    tag: 'Open SDK',
    label: 'Extensible Arena',
    sub: 'Custom agents, faults, evaluators, and reports',
  },
];

const features = [
  {
    icon: Network,
    tag: 'Realism',
    title: 'Environment Realism',
    description:
      'Runs SONiC-VS / Containerlab network state so agents diagnose symptoms produced by an operating test environment.',
  },
  {
    icon: Boxes,
    tag: 'Signals',
    title: 'Signal Coverage',
    description:
      'Each run exposes active probing and telemetry such as Pingmesh, BGP, syslog, interface counters, InfluxDB, and Grafana evidence.',
  },
  {
    icon: ChartColumnBig,
    tag: 'Interaction',
    title: 'Agent Interactivity',
    description:
      'Agents can use tools and runtime context to validate hypotheses before returning structured findings, rather than answering from a frozen prompt.',
  },
  {
    icon: ShieldCheck,
    tag: 'Open SDK',
    title: 'Open Extensibility',
    description:
      'The SDK supports custom agents, fault packs, evaluators, and reporting flows, so benchmark components can be replaced or extended.',
  },
];

const pipelineStages = [
  {
    stage: 'Stage 1',
    title: 'Stage a Live Arena',
    description:
      'NetOpsBench starts SONiC-VS switches in Containerlab, generates traffic, and injects controlled infrastructure faults.',
  },
  {
    stage: 'Stage 2',
    title: 'Expose Signals through Telemetry and Probing',
    description:
      'Active probes and observability streams surface latency, loss, routes, counters, logs, and topology evidence.',
  },
  {
    stage: 'Stage 3',
    title: 'Let Agents Interact with Evidence',
    description:
      'An RCA agent can inspect context, call tools, and correlate signals before producing a diagnosis result.',
  },
  {
    stage: 'Stage 4',
    title: 'Score and Compare Runs',
    description:
      'The benchmark scores detection, localization, runtime, tool use, and token cost against repeatable ground truth.',
  },
];

const heroKeywords = [
  'Reproducible',
  'Interactive Arena',
  'Open SDK',
  'Extensible',
  'Telemetry Signals',
];

const motivationCards = [
  {
    tag: 'Environment Feedback',
    title: 'Static datasets miss environment feedback',
    description:
      'Infrastructure agents need to validate hypotheses against changing runtime state, telemetry, and tool observations.',
  },
  {
    tag: 'Fair Replay',
    title: 'Incidents are hard to replay fairly',
    description:
      'Production incidents rarely provide a controlled baseline for comparing agents, prompts, tools, and scoring policies.',
  },
  {
    tag: 'Open Iteration',
    title: 'Closed benchmarks limit iteration',
    description:
      'Researchers need open extension points for agents, faults, evaluators, and future infrastructure domains.',
  },
];

const benchmarkCards = [
  {
    src: '/assets/benchmark/fig_avg_score.png',
    alt: 'Composite benchmark score across topology scales',
    caption: 'Composite score across XS, Small, Medium, and Large topologies',
  },
  {
    src: '/assets/benchmark/fig_device_loc.png',
    alt: 'Device localization accuracy by model',
    caption: 'Device-level localization performance under the benchmark corpus',
  },
  {
    src: '/assets/benchmark/fig_avg_time.png',
    alt: 'Average diagnosis time by model',
    caption: 'Average end-to-end diagnosis time per scenario',
  },
  {
    src: '/assets/benchmark/fig_tool_calls.png',
    alt: 'Average tool calls by model',
    caption: 'Tool usage efficiency during troubleshooting',
  },
];

export default function HomePage() {
  return (
    <main className={styles.homeRoot}>
      <section className={styles.hero}>
        <div className={styles.heroBg} />
        <div className={styles.heroContent}>
          <div className={styles.badge}>
            <span className={styles.badgeDot} />
            Network-first · SDK-extensible · AI infrastructure arena
          </div>
          <h1 className={styles.heroTitle}>
            NetOpsBench:{' '}
            <span className={styles.heroGradient}>An Interactive Arena for Agentic RCA in AI infrastructure</span>
          </h1>
          <p className={styles.heroSubtitle}>
            NetOpsBench provides a reproducible arena where agents diagnose live data-center network faults through telemetry, probing, tool interaction, and localization-first scoring.
          </p>
          <div className={styles.heroKeywords}>
            {heroKeywords.map((keyword) => (
              <span key={keyword} className={styles.heroKeyword}>
                {keyword}
              </span>
            ))}
          </div>
          <div className={styles.heroCta}>
            <Link href="/docs" className={styles.ctaPrimary}>
              Read Documentation
            </Link>
            <Link href="/docs/getting-started" className={styles.ctaSecondary}>
              Run Quick Start
            </Link>
            <a className={styles.ctaOutlined} href="https://github.com/NetX-lab/NetOpsBench">
              View on GitHub
            </a>
          </div>
          <div className={styles.heroMeta}>
            <span>Network-first DCN arena</span>
            <span>•</span>
            <span>SONiC-VS + Containerlab</span>
            <span>•</span>
            <span>Extensible toward broader AI infrastructure</span>
          </div>
        </div>
      </section>

      <section className={styles.statsSection}>
        <div className={styles.statsGrid}>
          {metrics.map((metric) => (
            <div key={metric.label} className={styles.statItem}>
              {metric.value != null ? (
                <div className={styles.statValue}>{metric.value}</div>
              ) : (
                <div className={styles.statTag}>{metric.tag}</div>
              )}
              <div className={styles.statLabel}>{metric.label}</div>
              {metric.sub && <div className={styles.statSub}>{metric.sub}</div>}
            </div>
          ))}
        </div>
      </section>

      <section className={styles.section}>
        <div className={styles.sectionInner}>
          <div className={styles.sectionLabel}>Motivation</div>
          <h2 className={styles.sectionTitle}>Network diagnosis agents need interactive evaluation</h2>
          <p className={styles.sectionDesc}>
            Despite Agentic RCA progress, 71% of large enterprises hesitate to fully automate
            network operations [1]. Network diagnosis needs repeatable arenas where agents can act on live signals, replay difficult faults, and compare scored outputs.
          </p>
          <div className={styles.motivationGrid}>
            {motivationCards.map((card) => (
              <article key={card.tag} className={styles.motivationCard}>
                <div className={styles.motivationCardTag}>{card.tag}</div>
                <h3 className={styles.motivationCardTitle}>{card.title}</h3>
                <p className={styles.motivationCardDesc}>{card.description}</p>
              </article>
            ))}
          </div>
          <div className={styles.propositionBox}>
            <p>
              <strong>NetOpsBench</strong> provides controlled, reproducible DCN fault environments
              with telemetry, probing, tool interaction, and <strong> localization-first scoring</strong>.
              Its <strong>open SDK</strong> defines extension points for custom agents, faults,
              evaluators, and reports.
            </p>
          </div>
          <div className={styles.archImgWrap}>
            <img
              className={styles.archImg}
              src={withBasePath('/assets/pipeline_architecture.png')}
              alt="NetOpsBench benchmark architecture"
            />
          </div>
        </div>
      </section>

      <section className={`${styles.section} ${styles.sectionAlt} ${styles.pipelineSection}`}>
        <div className={styles.sectionInner}>
          <div className={styles.sectionLabel}>Interactive Arena Loop</div>
          <h2 className={styles.sectionTitle}>An arena needs environment, signals, actions, and scoring</h2>
          <p className={styles.sectionDesc}>
            NetOpsBench implements this loop first for data-center networks, while the SDK keeps the arena open for new agents, fault packs, evaluators, and reporting workflows.
          </p>
          <div className={styles.pipelineGrid}>
            {pipelineStages.map((stage) => (
              <article key={stage.title} className={styles.pipelineCard}>
                <div className={styles.pipelineStage}>{stage.stage}</div>
                <h3 className={styles.pipelineTitle}>{stage.title}</h3>
                <p className={styles.pipelineDesc}>{stage.description}</p>
              </article>
            ))}
          </div>
          <div className={styles.pipelineLinks}>
            <Link href="/docs/operations/observability" className={styles.pipelinePrimaryLink}>
              Explore telemetry evidence
            </Link>
            <Link href="/docs/api/quickstart" className={styles.pipelineSecondaryLink}>
              Use the Python SDK
            </Link>
          </div>
        </div>
      </section>

      <section className={`${styles.section} ${styles.sectionAlt} ${styles.evaluationSection}`}>
        <div className={styles.sectionInner}>
          <div className={styles.sectionLabel}>Evaluation Dimensions</div>
          <h2 className={styles.sectionTitle}>What each run measures</h2>
          <p className={styles.sectionDesc}>
            Each run checks whether an agent can use available signals, interact with evidence, and return structured findings that can be scored against ground truth.
          </p>
          <div className={styles.featuresGrid}>
            {features.map((feature) => {
              const Icon = feature.icon;
              return (
                <article key={feature.title} className={styles.featureCard}>
                  <div className={styles.featureIconWrap}>
                    <Icon />
                  </div>
                  <div className={styles.featureTag}>{feature.tag}</div>
                  <h3 className={styles.featureTitle}>{feature.title}</h3>
                  <p className={styles.featureDesc}>{feature.description}</p>
                </article>
              );
            })}
          </div>
        </div>
      </section>

      <section className={styles.section}>
        <div className={styles.sectionInner}>
          <div className={styles.sectionLabel}>Benchmark results</div>
          <h2 className={styles.sectionTitle}>DCN benchmark results for diagnosis quality and efficiency</h2>
          <p className={styles.sectionDesc}>
            Published metrics report model performance on the current DCN benchmark across composite score, detection rate, diagnosis latency, and tool-use efficiency.
          </p>
          <div className={styles.benchmarkGrid}>
            {benchmarkCards.map((card) => (
              <figure key={card.src} className={styles.benchmarkCard}>
                <img className={styles.benchmarkImg} src={withBasePath(card.src)} alt={card.alt} />
                <figcaption className={styles.benchmarkCaption}>{card.caption}</figcaption>
              </figure>
            ))}
          </div>
          <div className={styles.centered}>
            <Link href="/docs/operations/observability" className={styles.ctaSecondary}>
              Explore observability guide
            </Link>
          </div>
        </div>
      </section>

      <section className={`${styles.section} ${styles.sectionAlt}`}>
        <div className={styles.sectionInner}>
          <div className={styles.sectionLabel}>Quick start</div>
          <h2 className={styles.sectionTitle}>Run one scenario and inspect one scored report</h2>
          <p className={styles.sectionDesc}>
            The fastest path to a first result is intentionally short and reproducible. Start from
            a clean environment, run one scenario, then inspect generated artifacts and scores.
          </p>
          <div className={styles.terminalWrap}>
            <div className={styles.terminalHeader}>
              <span className={styles.termDot} style={{ background: '#ef4444' }} />
              <span className={styles.termDot} style={{ background: '#f59e0b' }} />
              <span className={styles.termDot} style={{ background: '#22c55e' }} />
              <span className={styles.termTitle}>terminal</span>
            </div>
            <pre className={styles.terminalBody}>
              <code>
                <span className={styles.cmdLine}>
                  <span className={styles.cmdCommand}>git</span>{' '}
                  <span className={styles.cmdArg}>clone</span>{' '}
                  <span className={styles.cmdPath}>https://github.com/NetX-lab/NetOpsBench.git</span>
                </span>
                <span className={styles.cmdLine}>
                  <span className={styles.cmdCommand}>cd</span>{' '}
                  <span className={styles.cmdPath}>NetOpsBench</span>
                </span>
                <span className={styles.cmdLine}>
                  <span className={styles.cmdCommand}>python</span>{' '}
                  <span className={styles.cmdArg}>-m venv</span>{' '}
                  <span className={styles.cmdPath}>.venv</span>
                </span>
                <span className={styles.cmdLine}>
                  <span className={styles.cmdCommand}>source</span>{' '}
                  <span className={styles.cmdPath}>.venv/bin/activate</span>
                </span>
                <span className={styles.cmdLine}>
                  <span className={styles.cmdCommand}>pip</span>{' '}
                  <span className={styles.cmdArg}>install -e</span>{' '}
                  <span className={styles.cmdString}>".[agent]"</span>
                </span>
                <span className={styles.cmdLine}>
                  <span className={styles.cmdCommand}>export</span>{' '}
                  <span className={styles.cmdEnv}>OPENAI_API_KEY</span>
                  <span className={styles.cmdArg}>=...</span>
                </span>
                <span className={styles.cmdLine}>
                  <span className={styles.cmdEnv}>PYTHONPATH</span>
                  <span className={styles.cmdArg}>=.</span>{' '}
                  <span className={styles.cmdCommand}>python</span>{' '}
                  <span className={styles.cmdPath}>examples/01_run_scenario.py</span>{' '}
                  <span className={styles.cmdArg}>--vendor</span>{' '}
                  <span className={styles.cmdString}>openai</span>
                </span>
              </code>
            </pre>
          </div>
        </div>
      </section>

      <section className={styles.ctaBanner}>
        <div className={styles.sectionInner}>
          <h2 className={styles.ctaBannerTitle}>Build and evaluate diagnosis agents on a reproducible benchmark loop</h2>
          <p className={styles.ctaBannerDesc}>
            Start with the DCN arena, build an agent against live telemetry, then extend the benchmark through the open SDK.
          </p>
          <div className={styles.heroCta}>
            <Link href="/docs" className={styles.ctaBannerBtn}>
              Start with the DCN arena
            </Link>
            <Link href="/docs/build-your-agent/custom-agents" className={styles.ctaBannerBtn}>
              Build an agent
            </Link>
            <Link href="/docs/api/quickstart" className={styles.ctaBannerBtn}>
              Extend with the SDK
            </Link>
          </div>
          <p className={styles.citation}>
            [1] Broadcom, <i>2026 State of Network Operations Report</i>. Accessed May 2026.{' '}
            <a
              className={styles.citationLink}
              href="https://networkobservability.broadcom.com/hubfs/ESD/ESD_Microsites/AOD_Microsites_FY26/AOD_Microsites_FY26_Network%20Observability/AOD_Microsites_FY26_Network%20Observability_Files/Broadcom-2026-State-of-Network-Operations-Report.pdf"
              target="_blank"
              rel="noreferrer"
            >
              PDF
            </a>
            .
          </p>
        </div>
      </section>
    </main>
  );
}
