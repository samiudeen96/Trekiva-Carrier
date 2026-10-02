/**
 * `@shopify/app-bridge-types` registers its elements on the *global* `JSX` namespace, which
 * React 19's types no longer read (they use `React.JSX`). Re-expose the App Bridge element we
 * use on React's JSX namespace, reusing the attribute type shipped by the package.
 */
import type { UINavMenuAttributes } from '@shopify/app-bridge-types';
import type { RefAttributes } from 'react';

declare module 'react' {
  namespace JSX {
    interface IntrinsicElements {
      'ui-nav-menu': UINavMenuAttributes & RefAttributes<UINavMenuElement>;
    }
  }
}
