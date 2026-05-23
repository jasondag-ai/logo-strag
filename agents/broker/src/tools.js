import { supabase } from './db.js';

// caller is 'claude' or 'manus'. Manus relays human-submitted work, so its
// submissions count as 'human' in the schema.
export async function submitForReview({ caller, content, context, review_type }) {
  const submitter = caller === 'claude' ? 'claude' : 'human';
  const { data, error } = await supabase
    .from('review_queue')
    .insert({ submitter, content, context: context ?? null, review_type })
    .select('id, status')
    .single();
  if (error) throw new Error(`insert failed: ${error.message}`);
  return data;
}

// Pulls the oldest pending row and flips it to in_review in one statement.
// Returns null when the queue is empty.
export async function getPendingReview() {
  const { data, error } = await supabase.rpc('claim_next_review');
  if (error) throw new Error(`claim failed: ${error.message}`);
  const row = Array.isArray(data) ? data[0] : data;
  if (!row) return null;
  return {
    id: row.id,
    content: row.content,
    context: row.context,
    review_type: row.review_type,
  };
}

// status must be 'complete' (signed off) or 'pending' (bounced back to queue).
export async function submitReview({ id, findings, status }) {
  if (status !== 'complete' && status !== 'pending') {
    throw new Error("status must be 'complete' or 'pending'");
  }
  const update = {
    review_findings: findings,
    status,
    reviewed_at: status === 'complete' ? new Date().toISOString() : null,
  };
  const { error } = await supabase
    .from('review_queue')
    .update(update)
    .eq('id', id);
  if (error) throw new Error(`update failed: ${error.message}`);
  return { success: true };
}

export async function getReviewStatus({ id }) {
  const { data, error } = await supabase
    .from('review_queue')
    .select('status, review_findings, reviewed_at')
    .eq('id', id)
    .single();
  if (error) throw new Error(`fetch failed: ${error.message}`);
  return {
    status: data.status,
    findings: data.review_findings,
    reviewed_at: data.reviewed_at,
  };
}
