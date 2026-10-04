import React, {useRef, useState} from 'react';
import Link from '@docusaurus/Link';
import styles from './styles.module.css';

// The PR-Agent mark (the pull-request glyph from the favicon).
function Mark({className}) {
  return (
    <svg className={className} viewBox="0 0 16 16" aria-hidden="true">
      <path
        fill="currentColor"
        d="M1.5 3.25a2.25 2.25 0 1 1 3 2.122v5.256a2.251 2.251 0 1 1-1.5 0V5.372A2.25 2.25 0 0 1 1.5 3.25Zm5.677-.177L9.573.677A.25.25 0 0 1 10 .854V2.5h1A2.5 2.5 0 0 1 13.5 5v5.628a2.251 2.251 0 1 1-1.5 0V5a1 1 0 0 0-1-1h-1v1.646a.25.25 0 0 1-.427.177L7.177 3.427a.25.25 0 0 1 0-.354ZM3.75 2.5a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5Zm0 9.5a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5Zm8.25.75a.75.75 0 1 0 1.5 0 .75.75 0 0 0-1.5 0Z"
      />
    </svg>
  );
}

function Describe() {
  return (
    <>
      <dl className={styles.fields}>
        <dt>Type</dt>
        <dd>Enhancement</dd>
        <dt>Description</dt>
        <dd>
          <ul className={styles.bullets}>
            <li>Retry failed webhook deliveries with exponential backoff</li>
            <li>Record every attempt in the delivery log</li>
          </ul>
        </dd>
      </dl>
      <p className={styles.sub}>File walkthrough</p>
      <ul className={styles.files}>
        <li><code>webhooks/retry.py</code><span className={styles.add}>+48</span><span className={styles.del}>−6</span></li>
        <li><code>webhooks/handler.py</code><span className={styles.add}>+12</span><span className={styles.del}>−3</span></li>
        <li><code>tests/test_retry.py</code><span className={styles.add}>+71</span><span className={styles.del}>−0</span></li>
      </ul>
    </>
  );
}

function Review() {
  return (
    <>
      <p className={styles.heading}>PR Reviewer Guide</p>
      <ul className={styles.checks}>
        <li>
          <span>Estimated effort to review</span>
          <span className={styles.meter} role="img" aria-label="2 out of 5">
            {[1, 2, 3, 4, 5].map((n) => (
              <i key={n} className={n <= 2 ? styles.on : undefined} />
            ))}
          </span>
        </li>
        <li><span>PR contains tests</span></li>
        <li><span>No security concerns identified</span></li>
      </ul>
      <p className={styles.sub}>Recommended focus areas for review</p>
      <div className={styles.finding}>
        <code>webhooks/retry.py</code> <span className={styles.lines}>L42–51</span>
        <p>
          Unbounded backoff: <code>base * 2 ** attempt</code> has no cap, so the tenth retry waits
          more than eight minutes.
        </p>
      </div>
    </>
  );
}

function Improve() {
  return (
    <>
      <p className={styles.heading}>PR Code Suggestions</p>
      <div className={styles.suggestion}>
        <div className={styles.suggestionHead}>
          <span>Cap the retry delay</span>
          <span className={styles.tags}>
            <span>Possible issue</span>
            <span>Impact: High</span>
          </span>
        </div>
        <p>The delay doubles on every attempt with no upper bound. Clamp it so a long outage cannot stall the worker.</p>
        <pre className={styles.diff}>
          <span className={styles.diffFile}>webhooks/retry.py</span>
          <span className={styles.diffDel}>- delay = base * 2 ** attempt</span>
          <span className={styles.diffAdd}>+ delay = min(base * 2 ** attempt, MAX_DELAY)</span>
        </pre>
      </div>
    </>
  );
}

function Ask() {
  return (
    <p className={styles.answer}>
      Each delivery is retried up to <code>max_attempts</code> (10) times, doubling the delay from one
      second. The attempts span about 17 minutes; after the last one the event is marked{' '}
      <code>failed</code> in the delivery log and is not retried again.
    </p>
  );
}

const COMMANDS = [
  {id: 'describe', typed: '/describe', Reply: Describe, to: '/tools/describe/'},
  {id: 'review', typed: '/review', Reply: Review, to: '/tools/review/'},
  {id: 'improve', typed: '/improve', Reply: Improve, to: '/tools/improve/'},
  {id: 'ask', typed: '/ask "What happens if the endpoint is down for an hour?"', Reply: Ask, to: '/tools/ask/'},
];

const PROVIDERS = [
  {slug: 'github', name: 'GitHub'},
  {slug: 'gitlab', name: 'GitLab'},
  {slug: 'bitbucket', name: 'Bitbucket'},
  {slug: 'azuredevops', name: 'Azure DevOps'},
  {slug: 'gitea', name: 'Gitea'},
];

function Thread() {
  const [active, setActive] = useState(1);
  const tabs = useRef([]);
  const {typed, Reply, to, id} = COMMANDS[active];

  const onKeyDown = (event) => {
    const step = {ArrowRight: 1, ArrowLeft: -1}[event.key];
    if (!step) return;
    event.preventDefault();
    const next = (active + step + COMMANDS.length) % COMMANDS.length;
    setActive(next);
    tabs.current[next]?.focus();
  };

  return (
    <figure className={styles.thread} aria-label="Example: running a PR-Agent command on a pull request">
      <div className={styles.threadHead}>
        <span className={styles.prTitle}>feat: retry failed webhook deliveries</span>
        <span className={styles.prNumber}>#482</span>
      </div>

      <div className={styles.tabs} role="tablist" aria-label="Command" onKeyDown={onKeyDown}>
        {COMMANDS.map((command, index) => (
          <button
            key={command.id}
            ref={(el) => (tabs.current[index] = el)}
            type="button"
            role="tab"
            id={`pra-tab-${command.id}`}
            aria-selected={index === active}
            aria-controls="pra-thread-panel"
            tabIndex={index === active ? 0 : -1}
            className={styles.tab}
            onClick={() => setActive(index)}>
            /{command.id}
          </button>
        ))}
      </div>

      <div
        className={styles.panel}
        role="tabpanel"
        id="pra-thread-panel"
        aria-labelledby={`pra-tab-${id}`}>
        <div className={styles.comment} key={`${id}-typed`}>
          <span className={styles.avatar} aria-hidden="true">M</span>
          <div className={styles.commentBody}>
            <span className={styles.author}>maya</span>
            <code className={styles.typed}>{typed}</code>
          </div>
        </div>

        <div className={`${styles.comment} ${styles.reply}`} key={`${id}-reply`}>
          <span className={`${styles.avatar} ${styles.botAvatar}`} aria-hidden="true">
            <Mark className={styles.botMark} />
          </span>
          <div className={styles.commentBody}>
            <span className={styles.author}>
              pr-agent <span className={styles.bot}>bot</span>
            </span>
            <div className={styles.output}>
              <Reply />
            </div>
          </div>
        </div>
      </div>

      <Link className={styles.threadFoot} to={to}>
        How /{id} works →
      </Link>
    </figure>
  );
}

export default function Hero() {
  return (
    <header className={styles.hero}>
      <div className={styles.intro}>
        <h1 className={styles.title}>PR-Agent</h1>
        <p className={styles.lede}>
          An open-source AI agent that describes, reviews and improves pull requests. Run it from a PR
          comment, a webhook, your CI or the terminal.
        </p>
        <div className={styles.actions}>
          <Link className={styles.primary} to="/installation/">
            Install PR-Agent
          </Link>
          <Link className={styles.secondary} to="https://github.com/the-pr-agent/pr-agent">
            View on GitHub
          </Link>
        </div>
        <div className={styles.providers}>
          <span className={styles.providersLabel}>Works with</span>
          <ul className={styles.providerList}>
            {PROVIDERS.map(({slug, name}) => (
              <li key={slug} className="pra-provider">
                <span className={`pra-logo pra-logo--${slug}`} aria-hidden="true" />
                {name}
              </li>
            ))}
          </ul>
        </div>
      </div>
      <Thread />
    </header>
  );
}
