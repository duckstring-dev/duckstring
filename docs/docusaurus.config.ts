import {themes as prismThemes} from 'prism-react-renderer';
import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';

// This runs in Node.js - Don't use client-side code here (browser APIs, JSX...)

const config: Config = {
  title: 'Duckstring',
  tagline: 'Get your ducks in a row.',
  favicon: 'img/favicon.ico',

  future: {
    v4: true,
  },

  // The docs site doubles as the landing page until there's a commercial site, so the canonical
  // host is the apex domain (docs.duckstring.com points at the same deployment).
  url: 'https://duckstring.com',
  baseUrl: '/',

  organizationName: 'duckstring-dev',
  projectName: 'duckstring',

  onBrokenLinks: 'throw',

  // The Playground the landing page embeds (its story, /story). Under `docusaurus start` the page uses the
  // Playground's own dev server instead, on its reserved port (3001, playground/package.json; the docs keep 3000),
  // at whatever host the docs were opened on, so a phone on the LAN gets it too. That choice is made in the client
  // bundle (Landing/Story.tsx), not here: a production build rewrites the generated config a running dev server
  // shares. DUCKSTRING_PLAYGROUND_URL overrides both.
  customFields: {
    playgroundUrl: process.env.DUCKSTRING_PLAYGROUND_URL ?? 'https://playground.duckstring.com',
    playgroundDevPort: process.env.DUCKSTRING_PLAYGROUND_URL ? null : 3001,
  },

  i18n: {
    defaultLocale: 'en',
    locales: ['en'],
  },

  markdown: {
    format: 'detect',
    mermaid: true,
  },

  themes: ['@docusaurus/theme-mermaid'],
  plugins: [
    [
      // Pages from before the docs rewrite (and the removed Orchestration Theory spec), sent to where their
      // content lives now, since old links and search results still reach them.
      '@docusaurus/plugin-client-redirects',
      {
        redirects: [
          {to: '/concepts/orchestration', from: ['/concepts/freshness', '/getting-started/playground', '/old/concepts/freshness', '/old/getting-started/playground', '/reference/orchestration_theory', '/reference/theory', '/theory']},
          {to: '/concepts/trickles', from: ['/concepts/trickle', '/incremental-theory', '/old/concepts/trickle']},
          {to: '/concepts/catchments', from: ['/concepts/catchment', '/old/concepts/catchment']},
          {to: '/concepts/ponds', from: ['/concepts/versioning', '/old/concepts/versioning']},
          {to: '/concepts/management_and_execution', from: ['/old/reference/architecture', '/reference/architecture']},
          {to: '/', from: ['/intro']},
          {to: '/quickstart', from: ['/getting-started/installation', '/getting-started/quickstart', '/old/getting-started/installation', '/old/getting-started/quickstart']},
          {to: '/guides/testing_with_puddles', from: ['/concepts/puddles', '/guides/local-testing', '/old/concepts/puddles', '/old/guides/local-testing']},
          {to: '/guides/cloud_compute_on_aws', from: ['/guides/cloud', '/old/guides/cloud']},
          {to: '/guides/connecting_catchments', from: ['/guides/connecting-catchments', '/old/guides/connecting-catchments']},
          {to: '/guides/writing_ripples', from: ['/guides/creating-a-pond', '/old/guides/creating-a-pond']},
          {to: '/guides/dbt_projects', from: ['/guides/dbt', '/old/guides/dbt']},
          {to: '/guides/upgrades', from: ['/guides/deploying', '/old/guides/deploying']},
          {to: '/guides/ingesting_data', from: ['/guides/external-pipelines', '/old/guides/external-pipelines']},
          {to: '/guides/monitoring_and_failures', from: ['/guides/fault-tolerance', '/old/guides/fault-tolerance']},
          {to: '/guides/append_and_merge', from: ['/guides/incremental-ripples', '/guides/trickle', '/old/guides/incremental-ripples', '/old/guides/trickle']},
          {to: '/guides/querying', from: ['/guides/querying-data', '/old/guides/querying-data']},
          {to: '/guides/running_on_a_server', from: ['/guides/running-a-catchment', '/guides/web-ui', '/old/guides/running-a-catchment', '/old/guides/web-ui']},
          {to: '/guides/scheduling', from: ['/guides/triggers', '/guides/windows', '/old/guides/triggers', '/old/guides/windows']},
          {to: '/reference/cli/control', from: ['/guides/control', '/old/guides/control', '/reference/cli/do']},
          {to: '/reference/cli/data', from: ['/guides/lineage', '/old/guides/lineage', '/reference/cli/serve']},
          {to: '/reference/cli/pond', from: ['/reference/cli/puddle']},
          {to: '/reference/cli/refresh', from: ['/reference/cli/tap', '/reference/cli/trigger']},
          {to: '/reference/http_api', from: ['/old/reference/http-api', '/reference/http-api']},
          {to: '/reference/pond_toml', from: ['/old/reference/pond-toml', '/reference/pond-toml']},
          {to: '/reference/python/decorators', from: ['/old/reference/python-api', '/reference/python-api']},
          {to: '/concepts/ripples', from: ['/old/concepts/ripples']},
          {to: '/reference/cli', from: ['/old/reference/cli']},
        ],
      },
    ],
  ],

  presets: [
    [
      'classic',
      {
        docs: {
          routeBasePath: '/',
          sidebarPath: './sidebars.ts',
          remarkPlugins: [remarkMath],
          rehypePlugins: [rehypeKatex],
        },
        // Posts live in blog/ and are served at duckstring.com/blog. The blog moves to the commercial
        // site when duckstring.com stops serving this build, so every post must set an explicit `slug`:
        // URLs then don't depend on file names or Docusaurus's date-based routing, and survive the move.
        blog: {
          path: 'blog',
          routeBasePath: 'blog',
          blogTitle: 'Duckstring blog',
          blogDescription: 'Engineering notes on Duckstring and data engineering with DuckDB.',
          blogSidebarTitle: 'All posts',
          blogSidebarCount: 'ALL',
          showReadingTime: true,
          authorsMapPath: 'authors.yml',
          onInlineAuthors: 'throw',
          onUntruncatedBlogPosts: 'throw',
          // LaTeX in posts ($...$ inline, $$...$$ display), as in the docs. A literal dollar is written \$.
          remarkPlugins: [remarkMath],
          rehypePlugins: [rehypeKatex],
          feedOptions: {
            type: ['rss', 'atom'],
            xslt: true,
            copyright: `Copyright © ${new Date().getFullYear()} Duckstring.`,
          },
          processBlogPosts: async ({blogPosts}) => {
            const missing = blogPosts.filter((post) => !post.metadata.frontMatter.slug);
            if (missing.length > 0) {
              const files = missing.map((post) => post.metadata.source).join(', ');
              throw new Error(`Blog posts must set an explicit slug in their front matter: ${files}`);
            }
            return undefined;
          },
        },
        theme: {
          customCss: './src/css/custom.css',
        },
      } satisfies Preset.Options,
    ],
  ],

  themeConfig: {
    // Dark only: the logo mark and the landing palette are built for a dark canvas, and there is no
    // navbar on desktop to hold a toggle anyway. disableSwitch also drops it from the mobile navbar.
    colorMode: {
      defaultMode: 'dark',
      disableSwitch: true,
      respectPrefersColorScheme: false,
    },
    navbar: {
      title: 'Duckstring',
      logo: {
        alt: 'Duckstring',
        src: 'img/logo-mark.svg',
      },
      // The navbar is hidden on desktop docs pages (see custom.css) and only shown on blog pages,
      // which have no docs sidebar; these items are the way between the two.
      items: [
        {label: 'Docs', to: '/', activeBaseRegex: '^/(?!blog)'},
        {label: 'Blog', to: '/blog'},
      ],
    },
    footer: {
      style: 'dark',
      links: [
        {
          title: 'Docs',
          items: [
            {label: 'Concepts', to: '/concepts/ponds'},
            {label: 'Quickstart', to: '/quickstart'},
            {label: 'Orchestration', to: '/concepts/orchestration'},
            {label: 'Playground', href: 'https://playground.duckstring.com'},
          ],
        },
        {
          title: 'More',
          items: [
            {label: 'Blog', to: '/blog'},
            {label: 'GitHub', href: 'https://github.com/duckstring-dev/duckstring'},
            {label: 'Discussions', href: 'https://github.com/duckstring-dev/duckstring/discussions'},
            {label: 'Contact', href: 'mailto:dev@duckstring.com'},
          ],
        },
      ],
      copyright: `Copyright © ${new Date().getFullYear()} Duckstring.`,
    },
    prism: {
      theme: prismThemes.github,
      darkTheme: prismThemes.dracula,
      additionalLanguages: ['bash', 'toml'],
    },
  } satisfies Preset.ThemeConfig,
};

export default config;
