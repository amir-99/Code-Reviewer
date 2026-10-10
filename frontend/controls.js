/* Rerun and retry, read from the `controls` the API sends with every review.
   The API decides; this only turns its answer into what the buttons show, so a
   button never offers a request the API would refuse. */
import {FAILED, TERMINAL} from './flow.js';

const REFUSED = {allowed: false, reason: null};

export function runControls(review) {
  const controls = review?.controls || {};
  const rerun = controls.rerun || REFUSED;
  const retry = controls.retry || REFUSED;
  const failed = FAILED.has(review?.state);
  return {
    rerun: {
      show: Boolean(rerun.allowed),
      enabled: Boolean(rerun.allowed),
      reason: rerun.reason || '',
      // A rerun of a review still in flight supersedes it.
      confirm: Boolean(rerun.allowed) && !TERMINAL.has(review?.state),
    },
    retry: {
      // A failed review keeps offering retry when it is refused for a reason
      // its owner can act on (a later run, a moved head), so the reason shows.
      show: Boolean(retry.allowed) || (failed && Boolean(rerun.allowed)),
      enabled: Boolean(retry.allowed),
      reason: retry.reason || '',
    },
  };
}
