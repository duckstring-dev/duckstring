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
    {
      type: 'category',
      label: 'Guides',
      items: [
        {
          type: 'category',
          label: 'Building Ponds',
          items: [
            'guides/writing_ripples',
            'guides/sql_ripples',
            'guides/migrating_a_project',
            'guides/testing_with_puddles',
            'guides/dbt_projects',
          ],
        },
        {
          type: 'category',
          label: 'Incremental Ponds',
          items: [
            'guides/append_and_merge',
            'guides/joins_with_the_builder',
            'guides/aggregation_and_accumulation',
          ],
        },
        {
          type: 'category',
          label: 'Changing Ponds',
          items: ['guides/upgrades'],
        },
        {
          type: 'category',
          label: 'Running Pipelines',
          items: ['guides/scheduling', 'guides/monitoring_and_failures'],
        },
        {
          type: 'category',
          label: 'Getting Data Out',
          items: ['guides/querying', 'guides/delivering_with_spouts'],
        },
        {
          type: 'category',
          label: 'Operating Catchments',
          items: [
            'guides/running_on_a_server',
            'guides/hosting_on_a_platform',
            'guides/cloud_compute_on_aws',
            'guides/connecting_catchments',
          ],
        },
      ],
    },
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
    {type: 'link', label: 'Blog', href: '/blog'},
  ],
};

export default sidebars;
