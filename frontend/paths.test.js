import {test} from 'node:test';
import assert from 'node:assert/strict';
import {apiURL} from './paths.js';

test('API requests and SSE reconnects stay under the deployment prefix', () => {
  for (const prefix of ['/', '/agentic/']) {
    const moduleURL = `https://review.blubank.ai${prefix}paths.js`;
    for (const path of ['/admin/reviews', '/admin/models', '/admin/reviews/id/events?after=12']) {
      assert.equal(apiURL(path, moduleURL).href, `https://review.blubank.ai${prefix}api${path}`);
    }
  }
});
