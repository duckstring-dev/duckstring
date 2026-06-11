import {themes as prismThemes} from 'prism-react-renderer';
import type {Config} from '@docusaurus/types';
import type * as Preset from '@docusaurus/preset-classic';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';

// This runs in Node.js - Don't use client-side code here (browser APIs, JSX...)

const config: Config = {
  title: 'Duckstring',
  tagline: 'There is no DAG.',
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

  i18n: {
    defaultLocale: 'en',
    locales: ['en'],
  },

  markdown: {
    format: 'detect',
    mermaid: true,
  },

  themes: ['@docusaurus/theme-mermaid'],

  presets: [
    [
      'classic',
      {
        docs: {
          routeBasePath: '/',
          sidebarPath: './sidebars.ts',
          editUrl: 'https://github.com/duckstring-dev/duckstring/tree/main/docs/',
          remarkPlugins: [remarkMath],
          rehypePlugins: [rehypeKatex],
        },
        blog: false,
        theme: {
          customCss: './src/css/custom.css',
        },
      } satisfies Preset.Options,
    ],
  ],

  themeConfig: {
    colorMode: {
      respectPrefersColorScheme: true,
    },
    navbar: {
      title: 'Duckstring',
      logo: {
        alt: 'Duckstring',
        src: 'img/logo-mark.svg',
      },
      items: [
        {
          type: 'docSidebar',
          sidebarId: 'docs',
          position: 'left',
          label: 'Docs',
        },
        {
          href: 'https://playground.duckstring.com',
          label: 'Playground',
          position: 'right',
        },
        {
          href: 'https://github.com/duckstring-dev/duckstring',
          label: 'GitHub',
          position: 'right',
        },
      ],
    },
    footer: {
      style: 'dark',
      links: [
        {
          title: 'Docs',
          items: [
            {label: 'Introduction', to: '/intro'},
            {label: 'Quickstart', to: '/getting-started/quickstart'},
            {label: 'Theory', to: '/theory'},
          ],
        },
        {
          title: 'More',
          items: [
            {label: 'Playground', href: 'https://playground.duckstring.com'},
            {label: 'GitHub', href: 'https://github.com/duckstring-dev/duckstring'},
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
