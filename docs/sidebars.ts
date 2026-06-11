import type {SidebarsConfig} from '@docusaurus/plugin-content-docs';

const sidebars: SidebarsConfig = {
  docs: [
    'index',
    {
      type: 'category',
      label: 'Getting Started',
      collapsed: false,
      items: [
        'getting-started/playground',
        'getting-started/installation',
        'getting-started/quickstart',
      ],
    },
    {
      type: 'category',
      label: 'Concepts',
      collapsed: false,
      items: [
        'concepts/ponds',
        'concepts/ripples',
        'concepts/catchment',
        'concepts/freshness',
        'concepts/versioning',
      ],
    },
    {
      type: 'category',
      label: 'Guides',
      items: [
        'guides/running-a-catchment',
        'guides/creating-a-pond',
        'guides/deploying',
        'guides/triggers',
        'guides/windows',
        'guides/control',
        'guides/fault-tolerance',
        'guides/querying-data',
        'guides/web-ui',
      ],
    },
    'theory',
    {
      type: 'category',
      label: 'Reference',
      items: [
        'reference/cli',
        'reference/python-api',
        'reference/pond-toml',
        'reference/http-api',
        'reference/architecture',
      ],
    },
  ],
};

export default sidebars;
