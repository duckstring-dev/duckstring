import React from 'react';
import Link from '@docusaurus/Link';
import useBaseUrl from '@docusaurus/useBaseUrl';
import {useBlogPost} from '@docusaurus/plugin-content-blog/client';
import Content from '@theme-original/BlogPostItem/Content';
import type ContentType from '@theme/BlogPostItem/Content';
import type {WrapperProps} from '@docusaurus/types';

import styles from './styles.module.css';

type Props = WrapperProps<typeof ContentType>;

// A post's social image (`image` in its front matter) sits above its excerpt in the blog's listings, linked to the
// post, so every post shows the same picture there as in a shared link. On the post's own page it leads the post
// only with `image_on_page: true`, for an image that isn't already in the body. `image_alt` describes it (default:
// the title).
export default function ContentWrapper(props: Props): React.ReactElement {
  const {isBlogPostPage, metadata, frontMatter, assets} = useBlogPost();
  const extra = frontMatter as {image_on_page?: boolean; image_alt?: string};
  const source = assets.image ?? frontMatter.image;
  const src = useBaseUrl(source ?? '');
  const img = <img src={src} alt={extra.image_alt ?? metadata.title} loading={isBlogPostPage ? 'eager' : 'lazy'} />;
  return (
    <>
      {source && !isBlogPostPage && (
        <Link to={metadata.permalink} className={styles.cover}>
          {img}
        </Link>
      )}
      {source && isBlogPostPage && extra.image_on_page && <div className={styles.cover}>{img}</div>}
      <Content {...props} />
    </>
  );
}
