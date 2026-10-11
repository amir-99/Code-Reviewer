/* Stop, rerun and retry, read from the `controls` the API sends with every
   review. The API decides; this only turns its answer into what the buttons
   show, so a button never offers a request the API would refuse. */
import {FAILED, TERMINAL} from './flow.js';

const REFUSED = {allowed: false, reason: null};

export function runControls(review) {
  const controls = review?.controls || {};
  const stop = controls.stop || REFUSED;
  const rerun = controls.rerun || REFUSED;
  const retry = controls.retry || REFUSED;
  // Rerun is open to whoever may act on the review at all, so it stands in for
  // "this reader could use these controls" when one of the others is refused.
  const owner = Boolean(rerun.allowed);
  const state = review?.state;
  return {
    stop: {
      // A review publishing its report shows stop refused, with the reason.
      show: Boolean(stop.allowed) || (owner && Boolean(state) && !TERMINAL.has(state)),
      enabled: Boolean(stop.allowed),
      reason: stop.reason || '',
    },
    rerun: {
      show: owner,
      enabled: owner,
      reason: rerun.reason || '',
      // A rerun of a review still in flight supersedes it.
      confirm: owner && !TERMINAL.has(state),
    },
    retry: {
      // A failed review keeps offering retry when it is refused for a reason
      // its owner can act on (a later run, a moved head), so the reason shows.
      show: Boolean(retry.allowed) || (FAILED.has(state) && owner),
      enabled: Boolean(retry.allowed),
      reason: retry.reason || '',
    },
  };
}
