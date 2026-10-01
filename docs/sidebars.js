/**
 * Four sidebars, one per navbar tab. Groups are non-collapsible so every page in
 * a tab stays visible; each group answers one question a reader arrives with.
 *
 * The Overview (`index`) is the landing page at `/`, reached from the navbar logo,
 * so it is deliberately not listed here. Each tab opens its first entry.
 */

/** @type {import('@docusaurus/plugin-content-docs').SidebarsConfig} */
const sidebars = {
  // How do I start using it?
  getStarted: [
    {type: 'doc', id: 'usage-guide/introduction', label: 'Introduction'},
    {type: 'doc', id: 'overview/supported_platforms', label: 'Supported platforms'},
    {
      type: 'category',
      label: 'Installation',
      collapsible: false,
      link: {type: 'doc', id: 'installation/index'},
      // `className` draws the provider's logo before the label (provider-logos.css).
      items: [
        {type: 'doc', id: 'installation/locally', label: 'Locally', className: 'pra-side-provider pra-side-terminal'},
        {type: 'doc', id: 'installation/github', label: 'GitHub', className: 'pra-side-provider pra-side-github'},
        {type: 'doc', id: 'installation/gitlab', label: 'GitLab', className: 'pra-side-provider pra-side-gitlab'},
        {type: 'doc', id: 'installation/bitbucket', label: 'Bitbucket', className: 'pra-side-provider pra-side-bitbucket'},
        {type: 'doc', id: 'installation/azure', label: 'Azure DevOps', className: 'pra-side-provider pra-side-azuredevops'},
        {type: 'doc', id: 'installation/gitea', label: 'Gitea', className: 'pra-side-provider pra-side-gitea'},
      ],
    },
    {
      // Running without a hosted pull request, or as a service for another system.
      type: 'category',
      label: 'Other ways to run',
      collapsible: false,
      items: [
        {type: 'doc', id: 'usage-guide/local_git_provider', label: 'Local git provider'},
        {type: 'doc', id: 'usage-guide/plain_diff_mode', label: 'Plain-diff mode'},
        {type: 'doc', id: 'installation/mosaico_server', label: 'MOSAICO A2A server'},
      ],
    },
    {type: 'doc', id: 'overview/data_privacy', label: 'Data privacy'},
    {type: 'doc', id: 'faq/index', label: 'FAQ'},
  ],

  // How do I run and configure it day to day?
  guides: [
    {type: 'doc', id: 'usage-guide/index', label: 'Overview'},
    {
      type: 'category',
      label: 'Running PR-Agent',
      collapsible: false,
      items: [
        {type: 'doc', id: 'usage-guide/automations_and_usage', label: 'Usage and automation'},
        {type: 'doc', id: 'usage-guide/push_outputs', label: 'Push outputs'},
        {type: 'doc', id: 'usage-guide/mail_notifications', label: 'Mail notifications'},
      ],
    },
    {
      type: 'category',
      label: 'Configuration',
      collapsible: false,
      items: [
        {type: 'doc', id: 'usage-guide/configuration_options', label: 'Configuration file'},
        {type: 'doc', id: 'usage-guide/changing_a_model', label: 'Changing a model'},
        {type: 'doc', id: 'usage-guide/additional_configurations', label: 'Additional configurations'},
        {type: 'doc', id: 'usage-guide/custom_ca_and_self_signed_certificates', label: 'Custom CA certificates'},
        {type: 'doc', id: 'usage-guide/configuration_reference', label: 'Configuration reference'},
      ],
    },
    {
      type: 'category',
      label: 'Contributing',
      collapsible: false,
      items: [{type: 'doc', id: 'usage-guide/extending_pr_agent', label: 'Extending PR-Agent'}],
    },
  ],

  // What does each command do?
  tools: [
    {type: 'doc', id: 'tools/index', label: 'Overview'},
    {
      // The commands that run on a pull request by default or are used most.
      type: 'category',
      label: 'Main tools',
      collapsible: false,
      items: [
        {type: 'doc', id: 'tools/describe', label: 'Describe'},
        {type: 'doc', id: 'tools/review', label: 'Review'},
        {type: 'doc', id: 'tools/improve', label: 'Improve'},
        {type: 'doc', id: 'tools/ask', label: 'Ask'},
      ],
    },
    {
      type: 'category',
      label: 'More tools',
      collapsible: false,
      items: [
        {type: 'doc', id: 'tools/add_docs', label: 'Add docs'},
        {type: 'doc', id: 'tools/generate_labels', label: 'Generate labels'},
        {type: 'doc', id: 'tools/update_changelog', label: 'Update changelog'},
        {type: 'doc', id: 'tools/similar_issues', label: 'Similar issues'},
      ],
    },
    {
      type: 'category',
      label: 'Help',
      collapsible: false,
      items: [
        {type: 'doc', id: 'tools/help', label: 'Help'},
        {type: 'doc', id: 'tools/help_docs', label: 'Help docs'},
      ],
    },
  ],

  // How does it work under the hood?
  coreAbilities: [
    {type: 'doc', id: 'core-abilities/index', label: 'Overview'},
    {type: 'doc', id: 'core-abilities/agent_skills', label: 'Agent skills'},
    {type: 'doc', id: 'core-abilities/compression_strategy', label: 'Compression strategy'},
    {type: 'doc', id: 'core-abilities/dynamic_context', label: 'Dynamic context'},
    {type: 'doc', id: 'core-abilities/fetching_ticket_context', label: 'Fetching ticket context'},
    {type: 'doc', id: 'core-abilities/metadata', label: 'Local and global metadata'},
    {type: 'doc', id: 'core-abilities/self_reflection', label: 'Self-reflection'},
  ],
};

module.exports = sidebars;
