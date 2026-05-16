/** @type {import('@docusaurus/plugin-content-docs').SidebarsConfig} */
const sidebars = {
  getStarted: [
    {
      type: 'doc',
      id: 'index',
      label: 'Overview',
    },
    {
      type: 'doc',
      id: 'usage-guide/introduction',
      label: 'Introduction',
    },
    {
      type: 'category',
      label: 'Installation',
      collapsed: false,
      items: [
        { type: 'doc', id: 'installation/index', label: 'Overview' },
        { type: 'doc', id: 'installation/pr_agent', label: 'PR-Agent' },
        { type: 'doc', id: 'installation/locally', label: 'Locally' },
        { type: 'doc', id: 'installation/github', label: 'GitHub' },
        { type: 'doc', id: 'installation/gitlab', label: 'GitLab' },
        { type: 'doc', id: 'installation/bitbucket', label: 'Bitbucket' },
        { type: 'doc', id: 'installation/azure', label: 'Azure DevOps' },
        { type: 'doc', id: 'installation/gitea', label: 'Gitea' },
      ],
    },
    {
      type: 'category',
      label: 'Configuration & Usage',
      collapsed: false,
      link: { type: 'doc', id: 'usage-guide/index' },
      items: [
        { type: 'doc', id: 'usage-guide/configuration_options', label: 'Configuration File' },
        { type: 'doc', id: 'usage-guide/automations_and_usage', label: 'Usage and Automation' },
        { type: 'doc', id: 'usage-guide/changing_a_model', label: 'Changing a Model' },
        { type: 'doc', id: 'usage-guide/additional_configurations', label: 'Additional Configurations' },
        { type: 'doc', id: 'usage-guide/mail_notifications', label: 'Managing Mail Notifications' },
        { type: 'doc', id: 'usage-guide/EXAMPLE_BEST_PRACTICE', label: 'Example Best Practice' },
      ],
    },
  ],
  tools: [
    {
      type: 'doc',
      id: 'tools/index',
      label: 'Tools Overview',
    },
    { type: 'doc', id: 'tools/describe', label: 'Describe' },
    { type: 'doc', id: 'tools/review', label: 'Review' },
    { type: 'doc', id: 'tools/improve', label: 'Improve' },
    { type: 'doc', id: 'tools/ask', label: 'Ask' },
    { type: 'doc', id: 'tools/add_docs', label: 'Add Docs' },
    { type: 'doc', id: 'tools/generate_labels', label: 'Generate Labels' },
    { type: 'doc', id: 'tools/similar_issues', label: 'Similar Issues' },
    { type: 'doc', id: 'tools/help', label: 'Help' },
    { type: 'doc', id: 'tools/help_docs', label: 'Help Docs' },
    { type: 'doc', id: 'tools/update_changelog', label: 'Update Changelog' },
  ],
  coreAbilities: [
    {
      type: 'doc',
      id: 'core-abilities/index',
      label: 'Core Abilities Overview',
    },
    { type: 'doc', id: 'core-abilities/compression_strategy', label: 'Compression Strategy' },
    { type: 'doc', id: 'core-abilities/dynamic_context', label: 'Dynamic Context' },
    { type: 'doc', id: 'core-abilities/fetching_ticket_context', label: 'Fetching Ticket Context' },
    { type: 'doc', id: 'core-abilities/interactivity', label: 'Interactivity' },
    { type: 'doc', id: 'core-abilities/metadata', label: 'Local and Global Metadata' },
    { type: 'doc', id: 'core-abilities/self_reflection', label: 'Self-Reflection' },
  ],
  faq: [
    {
      type: 'doc',
      id: 'faq/index',
      label: 'Frequently Asked Questions',
    },
    {
      type: 'doc',
      id: 'overview/data_privacy',
      label: 'Data Privacy',
    },
  ],
};

module.exports = sidebars;
