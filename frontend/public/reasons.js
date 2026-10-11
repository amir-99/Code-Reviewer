/* Why a review failed, stopped or ran partial, from the `reasons` the API
   derives from what the run recorded. Older responses carry only `error`. */
import {FAILED, HALTED} from './flow.js';

export function explain(review) {
  if (!review) return null;
  const reasons = Array.isArray(review.reasons) ? review.reasons
    : review.error ? [{code: 'error', partial: false, message: review.error}] : [];
  if (FAILED.has(review.state)) {
    return {tone: 'bad', title: 'Why this review failed', items: reasons.length ? reasons
      : [{code: 'failed', partial: false, message: 'No reason was recorded.'}]};
  }
  if (HALTED.has(review.state)) return {tone: 'warn', title: 'Why this review stopped', items: reasons};
  if (review.partial) {
    // What made it partial first; what only limited it after.
    const ordered = [...reasons.filter(r => r.partial), ...reasons.filter(r => !r.partial)];
    return {tone: 'warn', title: 'Why this review is partial', items: ordered};
  }
  if (reasons.length) return {tone: 'info', title: 'What limited this review', items: reasons};
  return null;
}
