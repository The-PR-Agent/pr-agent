import React from 'react';
import Link from '@docusaurus/Link';
import styles from './styles.module.css';

// Doubles as navigation: the four commands are what a first-time visitor is
// actually looking for, and each links to its tool page.
const COMMANDS = [
  {cmd: '/describe', to: '/tools/describe/'},
  {cmd: '/review', to: '/tools/review/'},
  {cmd: '/improve', to: '/tools/improve/'},
  {cmd: '/ask', to: '/tools/ask/'},
];

const PROVIDERS = ['GitHub', 'GitLab', 'Bitbucket', 'Azure DevOps', 'Gitea'];

export default function Hero() {
  return (
    <header className={styles.hero}>
      <div className={styles.glow} aria-hidden="true" />

      <p className={styles.eyebrow}>
        <span className={styles.dot} aria-hidden="true" />
        Open source
      </p>

      <h1 className={styles.title}>PR-Agent</h1>

      <p className={styles.tagline}>
        An AI agent that reviews, describes and improves your pull requests —
        triggered from a PR comment, a webhook, a CI job, or your terminal.
      </p>

      <div className={styles.actions}>
        <Link className={styles.ctaPrimary} to="/installation/">
          Install PR-Agent
        </Link>
        <Link className={styles.ctaSecondary} to="/tools/">
          Browse the tools
        </Link>
        <Link className={styles.ctaGhost} to="https://github.com/the-pr-agent/pr-agent">
          GitHub
        </Link>
      </div>

      <ul className={styles.commands}>
        {COMMANDS.map(({cmd, to}) => (
          <li key={cmd}>
            <Link className={styles.command} to={to}>
              {cmd}
            </Link>
          </li>
        ))}
      </ul>

      <p className={styles.providers}>
        <span className={styles.providersLabel}>Works with</span>
        {PROVIDERS.map((name, i) => (
          <React.Fragment key={name}>
            {i > 0 && (
              <span className={styles.sep} aria-hidden="true">
                ·
              </span>
            )}
            <span className={styles.provider}>{name}</span>
          </React.Fragment>
        ))}
        <a className={styles.matrixLink} href="#features">
          full support matrix ↓
        </a>
      </p>
    </header>
  );
}
