// @ts-check

const {themes: prismThemes} = require('prism-react-renderer');

/**
 * One code surface in both colour modes, on the site's graphite (#11141a).
 *
 * Every popular *light* Prism theme fails WCAG AA on a light ground -- in
 * oneLight, comments are 2.40:1 and strings 2.93:1 -- and repainting most of
 * the token colours destroys the theme's internal harmony. oneDark keeps TOML
 * tables, keys, booleans and strings in four distinct hue families, which is
 * what these config-heavy docs need. Its own comment colour (#5c6370, 3.5:1)
 * is lifted, because our TOML samples carry real documentation in comments.
 */
const codeTheme = {
  plain: {color: '#d5d9e1', backgroundColor: '#11141a'},
  styles: [
    ...prismThemes.oneDark.styles,
    {types: ['comment', 'prolog', 'cdata'], style: {color: '#8a93a4', fontStyle: 'italic'}},
  ],
};

/** @type {import('@docusaurus/types').Config} */
const config = {
  title: 'PR-Agent',
  tagline: 'AI-powered code review agent',
  url: 'https://docs.pr-agent.ai',
  baseUrl: '/',
  // The MkDocs site published directory URLs, and pr_agent/tools/pr_help_message.py
  // emits them with a trailing slash into PR comments. Keep them byte-identical.
  trailingSlash: true,
  onBrokenLinks: 'throw',
  onBrokenAnchors: 'throw',
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
          customCss: [
            require.resolve('./src/css/custom.css'),
            require.resolve('./src/css/provider-logos.css'),
          ],
        },
      }),
    ],
  ],

  clientModules: [require.resolve('./src/fonts.js')],

  // kapa.ai "Ask AI" widget: https://docs.kapa.ai/integrations/website-widget/installation/docusaurus
  scripts: [
    {
      src: 'https://widget.kapa.ai/kapa-widget.bundle.js',
      'data-website-id': '5c89c0b1-b2ee-43c9-8c6f-43ed762b3c1e',
      'data-project-name': 'PR-Agent',
      'data-project-color': '#2459db',
      'data-project-logo': '/img/favicon.svg',
      // The launcher is filled with the project colour, so the blue mark needs a white variant there.
      'data-launcher-button-image': '/img/favicon-white.svg',
      // Follow the navbar's light/dark toggle instead of the OS preference.
      'data-color-scheme-selector': "[data-theme='dark']",
      async: true,
    },
  ],

  plugins: [
    [
      '@docusaurus/plugin-client-redirects',
      {
        redirects: [
          // '/summary/' was the GitBook table of contents, published by the MkDocs site.
          { from: '/summary', to: '/' },
          // A page of links that duplicated the Installation index.
          { from: '/installation/pr_agent', to: '/installation/' },
        ],
      },
    ],
    'docusaurus-plugin-image-zoom',
  ],

  themes: [
    [
      '@easyops-cn/docusaurus-search-local',
      /** @type {import("@easyops-cn/docusaurus-search-local").PluginOptions} */
      ({
        hashed: true,
        docsRouteBasePath: '/',
        indexBlog: false,
      }),
    ],
  ],

  themeConfig:
    /** @type {import('@docusaurus/preset-classic').ThemeConfig} */
    ({
      // Social preview card. MkDocs generated one per page via its `social` plugin;
      // a single static card keeps link previews branded without build-time imaging.
      image: 'img/social-card.png',
      metadata: [{name: 'twitter:card', content: 'summary_large_image'}],
      // Click-to-zoom for the UI screenshots (replaces the MkDocs glightbox plugin).
      zoom: {
        selector: '.markdown img:not(a img):not(.no-frame)',
        background: {
          light: 'rgba(248, 250, 252, 0.95)',
          dark: 'rgba(11, 18, 32, 0.95)',
        },
      },
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
            sidebarId: 'guides',
            label: 'Guides',
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
          {to: '/maintainers', label: 'Maintainers', position: 'left'},
          {
            href: 'https://github.com/the-pr-agent/pr-agent',
            label: 'GitHub',
            position: 'right',
          },
        ],
      },
      colorMode: {
        defaultMode: 'dark',
        respectPrefersColorScheme: true,
      },
      footer: {
        style: 'light',
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
        copyright: `Copyright \u00a9 ${new Date().getFullYear()} PR-Agent. Built with Docusaurus.`,
      },
      prism: {
        theme: codeTheme,
        darkTheme: codeTheme,
        additionalLanguages: ['toml', 'bash', 'yaml', 'python', 'json', 'ini', 'diff'],
      },
    }),
};

module.exports = config;
