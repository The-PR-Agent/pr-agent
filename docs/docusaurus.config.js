// @ts-check

/** @type {import('@docusaurus/types').Config} */
const config = {
  title: 'PR-Agent',
  tagline: 'AI-powered code review agent',
  url: 'https://docs.pr-agent.ai',
  baseUrl: '/',
  onBrokenLinks: 'throw',
  onBrokenAnchors: 'warn',
  favicon: 'img/favicon.svg',
  organizationName: 'the-pr-agent',
  projectName: 'pr-agent',

  markdown: {
    format: 'detect',
    hooks: {
      onBrokenMarkdownLinks: 'throw',
    },
  },

  presets: [
    [
      'classic',
      /** @type {import('@docusaurus/preset-classic').Options} */
      ({
        docs: {
          routeBasePath: '/',
          sidebarPath: require.resolve('./sidebars.js'),
          editUrl: 'https://github.com/the-pr-agent/pr-agent/tree/main/docs/',
        },
        blog: false,
        theme: {
          customCss: require.resolve('./src/css/custom.css'),
        },
      }),
    ],
  ],

  plugins: [
    [
      '@docusaurus/plugin-google-tag-manager',
      {
        containerId: 'GTM-5C9KZBM3',
      },
    ],
  ],

  themes: [
    [
      '@easyops-cn/docusaurus-search-local',
      /** @type {import("@easyops-cn/docusaurus-search-local").PluginOptions} */
      ({
        hashed: true,
        docsRouteBasePath: '/',
      }),
    ],
  ],

  themeConfig:
    /** @type {import('@docusaurus/preset-classic').ThemeConfig} */
    ({
      navbar: {
        title: 'PR-Agent',
        logo: {
          alt: 'PR-Agent Logo',
          src: 'img/favicon.svg',
        },
        items: [
          {
            type: 'docSidebar',
            sidebarId: 'getStarted',
            label: 'Get Started',
            position: 'left',
          },
          {
            type: 'docSidebar',
            sidebarId: 'tools',
            label: 'Tools',
            position: 'left',
          },
          {
            type: 'docSidebar',
            sidebarId: 'coreAbilities',
            label: 'Core Abilities',
            position: 'left',
          },
          {
            type: 'docSidebar',
            sidebarId: 'faq',
            label: 'FAQ',
            position: 'left',
          },
          {
            href: 'https://github.com/the-pr-agent/pr-agent',
            label: 'GitHub',
            position: 'right',
          },
        ],
      },
      announcementBar: {
        id: 'announcement',
        content:
          'Open source PR Agent documentation. For the Qodo free version, Get Started: <a href="https://www.qodo.ai/get-started/">https://www.qodo.ai/get-started/</a>',
        isCloseable: true,
      },
      colorMode: {
        defaultMode: 'light',
        respectPrefersColorScheme: true,
      },
      footer: {
        style: 'dark',
        links: [
          {
            title: 'Links',
            items: [
              {
                label: 'GitHub',
                href: 'https://github.com/the-pr-agent/pr-agent',
              },
            ],
          },
        ],
        copyright: `\u00a9 ${new Date().getFullYear()} PR-Agent Contributors`,
      },
      prism: {
        additionalLanguages: ['toml', 'bash', 'yaml', 'python', 'json'],
      },
    }),
};

module.exports = config;
