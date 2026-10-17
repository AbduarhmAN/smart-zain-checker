-- Only the backend may scan invoices already attached to an owned payment request.
begin;
create index pending_bound_invoice_page on commerce.payment_requests(id)
    where provider='cryptopay' and invoice_id is not null and payment_id is null;
create function commerce_rpc.pending_bound_invoices(p_after uuid default null,p_limit integer default 20)
returns jsonb language plpgsql stable security definer set search_path='' as $$
declare items jsonb;
begin
    if p_limit is null or p_limit not between 1 and 100 then
        raise exception 'Bounded invoice page required' using errcode='22023';end if;
    select coalesce(jsonb_agg(jsonb_build_object('quote_id',r.id,'telegram_user_id',u.telegram_user_id,
        'invoice_id',r.invoice_id) order by r.id),'[]') into items
        from (select pr.id,u.telegram_user_id,pr.invoice_id from commerce.payment_requests pr
              join commerce.users u on u.id=pr.user_id and u.registration_state='active'
              where pr.provider='cryptopay' and pr.invoice_id is not null and pr.payment_id is null
                and (p_after is null or pr.id>p_after) order by pr.id limit p_limit) r
        join commerce.users u on u.telegram_user_id=r.telegram_user_id;
    return items;
end $$;
revoke all on function commerce_rpc.pending_bound_invoices(uuid,integer) from public,anon,authenticated;
grant execute on function commerce_rpc.pending_bound_invoices(uuid,integer) to service_role;
create or replace function commerce_rpc.health()
returns jsonb language sql stable security definer set search_path='' as $$
    select jsonb_build_object('status','ok','schema_version',26,'currency','EGP')
$$;
notify pgrst,'reload schema';
commit;
