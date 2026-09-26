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
    // {
    //   type: 'category',
    //   label: 'Reference',
    //   items: [
    //     // TODO
    //   ],
    // },
  ],
};

export default sidebars;
