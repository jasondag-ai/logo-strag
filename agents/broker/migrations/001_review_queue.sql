-- gen_random_uuid lives in pgcrypto. Supabase ships it; this is defensive.
create extension if not exists "pgcrypto";

create table if not exists review_queue (
  id              uuid primary key default gen_random_uuid(),
  submitted_at    timestamptz not null default now(),
  submitter       text not null check (submitter in ('claude', 'human')),
  content         jsonb not null,
  context         text,
  review_type     text not null check (review_type in ('code', 'doc', 'strategy', 'deal')),
  status          text not null default 'pending' check (status in ('pending', 'in_review', 'complete')),
  review_findings jsonb,
  reviewed_at     timestamptz
);

create index if not exists review_queue_status_idx       on review_queue (status);
create index if not exists review_queue_review_type_idx  on review_queue (review_type);

-- Atomic claim. SELECT FOR UPDATE SKIP LOCKED keeps concurrent pulls safe.
create or replace function claim_next_review()
returns setof review_queue
language plpgsql
as $$
begin
  return query
  update review_queue
  set status = 'in_review'
  where id = (
    select id from review_queue
    where status = 'pending'
    order by submitted_at asc
    limit 1
    for update skip locked
  )
  returning *;
end;
$$;
