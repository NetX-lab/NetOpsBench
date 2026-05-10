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
    tag: 'Fault Injection',
    label: 'Controlled Fault Injection',
    sub: '5 families · 12 built-in types · link / routing / impairment / ACL / system',
  },
  {
    value: null,
    tag: 'Full-Stack',
    label: 'Observability Coverage',
    sub: 'Pingmesh · BGP · syslog · Telegraf · Grafana',
  },
  {
    value: null,
    tag: 'Open SDK',
    label: 'Extensible Platform',
    sub: 'Stable API for custom agents, faults & evaluators',
  },
];

const features = [
  {
    icon: Network,
    tag: 'Localization',
    title: 'Alert-Driven Fault Localization',
    description:
      'Diagnosis starts from end-to-end alert symptoms, then localizes candidate fault domains to concrete devices and interfaces instead of only producing coarse verdicts.',
  },
  {
    icon: Boxes,
    tag: 'Runtime',
    title: 'Observability Designed into Runtime',
    description:
      'Each run wires Telegraf, InfluxDB, Grafana, Pingmesh, syslog, and BGP collection so diagnosis is grounded in real signals rather than synthetic hints.',
  },
  {
    icon: ChartColumnBig,
    tag: 'Reasoning',
    title: 'Symptom-to-Judgment Agent Workflow',
    description:
      'The benchmark loop preserves the full troubleshooting path from observed symptoms to final verdict, making agent reasoning behavior inspectable and testable.',
  },
  {
    icon: ShieldCheck,
    tag: 'Scoring',
    title: 'Operations-Aligned Scoring',
    description:
      'Scoring tracks detection, localization, runtime, tool-use, and token efficiency to better reflect practical troubleshooting value in production operations.',
  },
];

const pipelineStages = [
  {
    stage: 'Stage 1',
    title: 'Pingmesh Detects Path Symptoms',
    description:
      'End-to-end active probes capture RTT/loss degradation and surface path-level symptoms across racks.',
  },
  {
    stage: 'Stage 2',
    title: 'Alert Frames Suspected Scope',
    description:
      'Alert evidence defines likely fault scope and narrows diagnosis search space before deep tool calls.',
  },
  {
    stage: 'Stage 3',
    title: 'Agent Correlates Multi-Source Signals',
    description:
      'The agent reasons over Pingmesh, BGP, syslog, and interface telemetry to form a localized hypothesis.',
  },
  {
    stage: 'Stage 4',
    title: 'Benchmark Scores Localization Quality',
    description:
      'Detection and localization outputs are scored against ground truth to quantify practical troubleshooting value.',
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
    <main>
      <section className={styles.hero}>
        <div className={styles.heroBg} />
        <div className={styles.heroContent}>
          <div className={styles.badge}>
            <span className={styles.badgeDot} />
            Open Source Benchmark Infrastructure
          </div>
          <h1 className={styles.heroTitle}>
            NetOpsBench for{' '}
            <span className={styles.heroGradient}>Agentic Network Fault Diagnosis</span>
          </h1>
          <p className={styles.heroSubtitle}>
            A production-grade benchmark infrastructure for data-center troubleshooting. NetOpsBench orchestrates a closed-loop evaluation: injecting topology faults, triggering Pingmesh-driven active alerts, and enforcing evidence-based scoring to quantify agentic localization capabilities.
          </p>
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
            <span>SONiC-VS</span>
            <span>•</span>
            <span>Containerlab</span>
            <span>•</span>
            <span>Pingmesh alert-driven localization</span>
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

      <section className={`${styles.section} ${styles.sectionAlt} ${styles.pipelineSection}`}>
        <div className={styles.sectionInner}>
          <div className={styles.sectionLabel}>Alert-Driven Localization Pipeline</div>
          <h2 className={styles.sectionTitle}>Diagnosis starts from Pingmesh end-to-end symptoms</h2>
          <p className={styles.sectionDesc}>
            NetOpsBench enforces a mechanism-first workflow: Pingmesh active probes trigger diagnosis, prioritizing concrete fault domain localization over coarse verdicts to keep outcomes tied to operational evidence.
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
              Pingmesh observability evidence
            </Link>
            <Link href="/docs/operations/deployment" className={styles.pipelineSecondaryLink}>
              Pingmesh deployment path
            </Link>
          </div>
        </div>
      </section>

      <section className={styles.section}>
        <div className={styles.sectionInner}>
          <div className={styles.sectionLabel}>Research-Industry Gap</div>
          <h2 className={styles.sectionTitle}>Agentic RCA lacks production-grade evaluation</h2>
          <p className={styles.sectionDesc}>
            Despite Agentic RCA progress, 71% of large enterprises hesitate to fully automate network operations [1]. NetOpsBench bridges this gap by providing an evidence-oriented, reproducible benchmark for LLM-driven diagnosis.
          </p>
          <div className={styles.archImgWrap}>
            <img
              className={styles.archImg}
              src={withBasePath('/assets/pipeline_architecture.png')}
              alt="NetOpsBench benchmark architecture"
            />
          </div>
        </div>
      </section>

      <section className={`${styles.section} ${styles.sectionAlt} ${styles.evaluationSection}`}>
        <div className={styles.sectionInner}>
          <div className={styles.sectionLabel}>Evaluation Dimensions</div>
          <h2 className={styles.sectionTitle}>Core dimensions for trustworthy diagnosis evaluation</h2>
          <p className={styles.sectionDesc}>
            NetOpsBench evaluates diagnosis quality through four complementary dimensions: fault localization rigor, runtime observability adequacy, symptom-to-judgment reasoning traceability, and operations-aligned scoring realism.
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
          <h2 className={styles.sectionTitle}>Quantitative evidence for diagnosis quality and efficiency</h2>
          <p className={styles.sectionDesc}>
            Published metrics quantify model performance across composite accuracy, detection rate, diagnosis latency, and tool-use efficiency, enabling horizontal comparison under a consistent runtime.
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
            Use NetOpsBench as a shared evaluation substrate for Agentic RCA research and
            engineering. Compare methods with consistent scenarios, telemetry, and scoring.
          </p>
          <div className={styles.heroCta}>
            <Link href="/docs" className={styles.ctaBannerBtn}>
              Open docs
            </Link>
            <a className={styles.ctaBannerBtn} href="https://github.com/NetX-lab/NetOpsBench">
              View repository
            </a>
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
