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
          ],
        },
        {
          title: 'More',
          items: [
            {label: 'Blog', to: '/blog'},
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
