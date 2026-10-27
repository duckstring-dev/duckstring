import React from 'react';
import Link from '@docusaurus/Link';
import useBaseUrl from '@docusaurus/useBaseUrl';
import {useBlogPost} from '@docusaurus/plugin-content-blog/client';
import Content from '@theme-original/BlogPostItem/Content';
import type ContentType from '@theme/BlogPostItem/Content';
import type {WrapperProps} from '@docusaurus/types';

import styles from './styles.module.css';

type Props = WrapperProps<typeof ContentType>;

// In the blog's listings, a post's social image (`image` in its front matter) sits above its excerpt, linked to the
// post, so every post shows the same picture there as in a shared link. The post's own page is unchanged.
export default function ContentWrapper(props: Props): React.ReactElement {
  const {isBlogPostPage, metadata, frontMatter, assets} = useBlogPost();
  const image = useBaseUrl(assets.image ?? frontMatter.image ?? '');
  return (
    <>
      {!isBlogPostPage && (assets.image ?? frontMatter.image) && (
        <Link to={metadata.permalink} className={styles.cover}>
          <img src={image} alt={metadata.title} loading="lazy" />
        </Link>
      )}
      <Content {...props} />
    </>
  );
}
