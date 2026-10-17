-- Deferred validation runs at commit, after the RPC's definer scope has ended.
-- Its private ledger read must use the function owner's authority, without
-- granting the runtime role any direct schema or financial-table access.
begin;
alter function commerce.enforce_balanced_journal() security definer;
alter function commerce.enforce_balanced_journal() set search_path = '';
revoke all on function commerce.enforce_balanced_journal() from public,anon,authenticated,service_role;
create or replace function commerce_rpc.health()
returns jsonb language sql stable security definer set search_path='' as $$
    select jsonb_build_object('status','ok','schema_version',28,'currency','EGP')
$$;
notify pgrst,'reload schema';
commit;
