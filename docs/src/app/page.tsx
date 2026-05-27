import Link from 'next/link';

import styles from './page.module.css';
import { withBasePath } from '@/lib/base-path';

const metrics = [
  {
    value: null,
    tag: 'Reproducible',
    label: 'Fair Benchmarks',
    sub: 'Controlled fault cases with scenario ground truth',
  },
  {
    value: null,
    tag: 'Interactive',
    label: 'Realistic Environment',
    sub: 'Agents operate against runtime state, not static logs',
  },
  {
    value: null,
    tag: 'Observability',
    label: 'Tracing & Telemetry',
    sub: 'Pingmesh · BGP · Syslog · counters · Grafana',
  },
  {
    value: null,
    tag: 'Open SDK',
    label: 'Extensible Arena',
    sub: 'Custom agents, faults, evaluators, and reports',
  },
];

const pipelineStages = [
  {
    stage: 'Environment',
    title: 'Scalable Live Networks',
    description:
      'Supports emulation of mainstream data-center network live environments, including spine-leaf, fat-tree and rail-optimized topologies.',
  },
  {
    stage: 'Evidence',
    title: 'Full observability accessible to your agent',
    description:
      'Pingmesh, BGP state, gNMI counters, switch CLI output, syslog, and Grafana-backed telemetry are available through runtime services and MCP tools.',
  },
  {
    stage: 'Interaction',
    title: 'Reproducible fault injection and agent interaction',
    description:
      'Scenarios inject controlled faults and automatically trigger the diagnosis loop, giving each agent the same fault window, topology, and evidence surface.',
  },
  {
    stage: 'Scoring',
    title: 'Accuracy and efficiency scoring',
    description:
      'Every diagnosis is automatically evaluated based on detection accuracy and operational efficiency, enabling fair comparisons between different strategies.',
  },
];

const motivationCards = [
  {
    tag: 'Fair Evaluation',
    title: 'Lack of Fair Comparison Across Different Agents',
    description:
      'Varied network topologies, fault sets, observability tools and evaluation metrics hinder the comparison of agentic troubleshooting strategies across the research community.',
  },
  {
    tag: 'Fault Reproducibility',
    title: 'Lack of Reproducible Real-World Incidents',
    description:
      'Real network incidents cannot be reliably reproduced or labeled with consistent ground truth, slowing iterative improvement and evaluation of troubleshooting agents.',
  },
  {
    tag: 'Interactive Environment',
    title: 'Static Logs Cannot Support Agentic Troubleshooting',
    description:
      'Static topology snapshots and logs cannot provide live probing and telemetry signals required by agents for diagnostic work.',
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
            Open arena · Fair benchmarks · AI infrastructure
          </div>
          <h1 className={styles.heroTitle}>
            <span className={styles.heroTitleLine}>
              NetOpsBench: <span className={styles.heroGradient}>Open Arena</span>
            </span>
            <span className={`${styles.heroTitleLine} ${styles.heroGradient}`}>
              for NetOps in AI Infrastructure
            </span>
          </h1>
          <p className={styles.heroByline}>
            A reproducible benchmark for agentic network troubleshooting.
          </p>
          <p className={styles.heroSubtitle}>
            NetOpsBench evaluated agentic network troubleshooting in modern AI infrastructure. The platform supports the emulation of diverse live, interactive data-center network environments, injects controlled and reproducible faults, and evaluates custom agent strategies against ground truth.
          </p>
          <div className={styles.heroCta}>
            <Link href="/docs/quickstart" className={styles.ctaSecondary}>
              Run Quickstart
            </Link>
            <a className={styles.ctaOutlined} href="https://github.com/NetX-lab/NetOpsBench">
              View on GitHub
            </a>
            <Link href="/docs" className={styles.ctaPrimary}>
              Read Documentation
            </Link>
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
          <h2 className={styles.sectionTitle}>Why agentic benchmarks are needed for NetOps?</h2>
          <p className={styles.sectionDesc}>
            According to the Broadcom 2026 State of Network Operations Report, 71% of large enterprises do not fully trust AI-based network operations, and only 27% have mature automation practices. The primary barrier is not insufficient agentic strategies, but the lack of reproducible, reliable <strong>benchmarks and evaluation environments</strong> to guide the iteration and validation of these strategies.
          </p>
          <img
            className={styles.motivationImg}
            src={withBasePath('/assets/Motivation.png')}
            alt="Three gaps that block agentic NetOps deployment"
          />
          <div className={styles.motivationGrid}>
            {motivationCards.map((card) => (
              <article key={card.tag} className={styles.motivationCard}>
                <div className={styles.motivationCardTag}>{card.tag}</div>
                <h3 className={styles.motivationCardTitle}>{card.title}</h3>
                <p className={styles.motivationCardDesc}>{card.description}</p>
              </article>
            ))}
          </div>
        </div>
      </section>

      <section className={`${styles.section} ${styles.sectionAlt} ${styles.pipelineSection}`}>
        <div className={styles.sectionInner}>
          <div className={styles.sectionLabel}>Benchmark workflow</div>
          <h2 className={styles.sectionTitle}>Plug in your custom agent and launch evaluations</h2>
          <p className={styles.sectionDesc}>
            NetOpsBench builds <strong>open, fair, and publicly available</strong> arenas where you can test your agentic strategies and obtain objective, reproducible performance results.
          </p>
          <img
              className={styles.motivationImg}
              src={withBasePath('/assets/pipeline_architecture.png')}
              alt="NetOpsBench benchmark architecture"
            />
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
            <Link href="/docs/run-benchmarks/results" className={styles.pipelinePrimaryLink}>
              See benchmark results
            </Link>
            <Link href="/docs/build-your-agent/custom-agents" className={styles.pipelineSecondaryLink}>
              Build your own agent
            </Link>
          </div>
        </div>
      </section>

      <section className={`${styles.section} ${styles.sectionAlt}`}>
        <div className={styles.sectionInner}>
          <div className={styles.sectionLabel}>Quick start</div>
          <h2 className={styles.sectionTitle}>Run one scenario and inspect one scored report</h2>
          <p className={styles.sectionDesc}>
            Start from a clean environment, run one scenario, then inspect generated artifacts and scores.
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
          <h2 className={styles.ctaBannerTitle}>Evaluate your own agentic RCA in NetOpsBench</h2>
          <p className={styles.ctaBannerDesc}>
            Start from the Quickstart, then swap in your own diagnose(context) implementation.
          </p>
          <div className={styles.heroCta}>
            <Link href="/docs/quickstart" className={styles.ctaBannerBtn}>
              Quickstart
            </Link>
            <Link href="/docs/build-your-agent/custom-agents" className={styles.ctaBannerBtn}>
              Build an agent
            </Link>
            <Link href="/docs/build-your-agent/python-api-guide" className={styles.ctaBannerBtn}>
              Python API
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
