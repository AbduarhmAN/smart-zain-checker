-- Production pg_safeupdate requires targeted updates. Preserve applied 018.
begin;
create or replace function commerce_rpc.configure_support_actors(p_actor_ids jsonb,p_reference text)
returns jsonb language plpgsql security definer set search_path='' as $$
declare actor jsonb;
begin
    if p_actor_ids is null or jsonb_typeof(p_actor_ids)<>'array' or jsonb_array_length(p_actor_ids) not between 1 and 30
       or p_reference is null or length(btrim(p_reference)) not between 1 and 1000 then
        raise exception 'Verified local support administrators required' using errcode='22023'; end if;
    for actor in select value from jsonb_array_elements(p_actor_ids) loop
        if jsonb_typeof(actor)<>'number' or actor::text !~ '^[1-9][0-9]{0,15}$'
           or actor::text::numeric>=4503599627370496 then raise exception 'Invalid support administrator identity' using errcode='22023'; end if;
    end loop;
    perform pg_advisory_xact_lock(610140018);
    update commerce.support_actors set active=false,selection_reference=p_reference,updated_at=now()
        where active and telegram_user_id not in(select (value::text)::bigint from jsonb_array_elements(p_actor_ids));
    insert into commerce.support_actors(telegram_user_id,active,selection_reference)
        select distinct (value::text)::bigint,true,p_reference from jsonb_array_elements(p_actor_ids)
        on conflict(telegram_user_id) do update set active=true,selection_reference=excluded.selection_reference,updated_at=now();
    return jsonb_build_object('state','configured','active_count',(select count(*) from commerce.support_actors where active));
end $$;
revoke all on function commerce_rpc.configure_support_actors(jsonb,text) from public,anon,authenticated;
grant execute on function commerce_rpc.configure_support_actors(jsonb,text) to service_role;
create or replace function commerce_rpc.health()
returns jsonb language sql stable security definer set search_path='' as $$
    select jsonb_build_object('status','ok','schema_version',19,'currency','EGP')
$$;
notify pgrst,'reload schema';
commit;
