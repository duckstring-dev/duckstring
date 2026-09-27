import type {SidebarsConfig} from '@docusaurus/plugin-content-docs';

const sidebars: SidebarsConfig = {
  docs: [
    'index',
    'quickstart',
    {
      type: 'category',
      label: 'Concepts',
      collapsed: false,
      items: [
        'concepts/ponds',
        'concepts/ripples',
        'concepts/catchments',
        'concepts/orchestration',
        'concepts/trickles',
        'concepts/management_and_execution',
      ],
    },
    // {
    //   type: 'category',
    //   label: 'Guides',
    //   items: [
    //     // TODO
    //   ],
    // },
    {
      type: 'category',
      label: 'Reference',
      items: [
        'reference/orchestration_theory',
        {
          type: 'category',
          label: 'Python API',
          items: [
            'reference/python/decorators',
            'reference/python/pond',
            'reference/python/trickle_io',
            'reference/python/trickle_builder',
            'reference/python/agg',
            'reference/python/acc',
            'reference/python/puddle',
            'reference/python/catchment',
          ],
        },
        'reference/pond_toml',
        {
          type: 'category',
          label: 'CLI',
          items: [
            'reference/cli/index',
            'reference/cli/catchment',
            'reference/cli/pond',
            'reference/cli/puddle',
            'reference/cli/trigger',
            'reference/cli/control',
            'reference/cli/do',
            'reference/cli/data',
            'reference/cli/duck',
            'reference/cli/spout',
            'reference/cli/serve',
            'reference/cli/secret',
            'reference/cli/alert',
          ],
        },
        'reference/http_api',
        'reference/formats',
        'reference/environment',
      ],
    },
  ],
};

export default sidebars;
