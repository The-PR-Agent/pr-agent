import React from 'react';
import Layout from '@theme/Layout';
import Link from '@docusaurus/Link';
import {maintainers} from '@site/src/data/maintainers';
import styles from './maintainers.module.css';

function MaintainerCard({login, name, role}) {
  return (
    <li className={styles.card}>
      <img
        className={styles.avatar}
        src={`https://avatars.githubusercontent.com/${login}?s=160`}
        alt=""
        width="80"
        height="80"
        loading="lazy"
      />
      <Link className={styles.name} to={`https://github.com/${login}`}>
        {name}
      </Link>
      <span className={styles.role}>{role}</span>
      <span className={styles.handle}>
        <span className="pra-logo pra-logo--github" aria-hidden="true" />@{login}
      </span>
    </li>
  );
}

export default function Maintainers() {
  return (
    <Layout
      title="Maintainers"
      description="The people who maintain PR-Agent, the open-source AI agent for pull requests.">
      <main className={styles.page}>
        <header className={styles.header}>
          <h1 className={styles.title}>Maintainers</h1>
          <p className={styles.lede}>
            PR-Agent was started at Qodo (then CodiumAI) in July 2023. In April 2026 Qodo donated it to
            the open-source community, and it now lives in the PR-Agent organization on GitHub, maintained
            by the people below.
          </p>
          <p className={styles.join}>
            The project is open to new contributors and maintainers.{' '}
            <Link to="https://github.com/the-pr-agent/pr-agent/blob/main/CONTRIBUTING.md">
              Read the contributing guide
            </Link>
          </p>
        </header>
        <ul className={styles.grid}>
          {maintainers.map((maintainer) => (
            <MaintainerCard key={maintainer.login} {...maintainer} />
          ))}
        </ul>
      </main>
    </Layout>
  );
}
