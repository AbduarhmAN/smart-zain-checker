-- Synthetic permissions: no existing production partner or financial state touched.
begin;
do $$
declare pid uuid;other uuid;pending uuid;approved uuid;funded uuid;invoiced uuid;ordinary uuid;enabled_order uuid;
    price bigint;policy bigint;result jsonb;before_order jsonb;before_journals bigint;before_terms jsonb;
    rows jsonb:='[{"row_number":2,"check_type":"account"},{"row_number":3,"check_type":"account"}]';
    source jsonb:=jsonb_build_object('file_sha256',repeat('f',64),'original_filename','access.xlsx');
begin
    perform commerce_rpc.configure_support_actors('[9,21]'::jsonb,'sheet-access-admins');
    pid:=(commerce_rpc.create_partner(899801,'ACCESS_PARTNER')->>'partner_id')::uuid;
    other:=(commerce_rpc.create_partner(899802,'ACCESS_OTHER')->>'partner_id')::uuid;
    perform commerce_rpc.configure_partner(pid,9,3000,'forever',null,true);
    perform commerce_rpc.configure_partner(other,9,3000,'forever',null,true);
    select to_jsonb(p) into before_terms from commerce.partners p where id=pid;
    assert commerce_rpc.partner_sheet_context(899801)->>'partner_sheet_enabled'='true';
    price:=(commerce_rpc.configure_partner_sheet_price(9,1500,'access-price')->>'version')::bigint;
    pending:=(commerce_rpc.create_order(899801,'access-pending',rows,source)->>'order_id')::uuid;
    approved:=(commerce_rpc.create_order(899801,'access-approved',rows,source)->>'order_id')::uuid;
    funded:=(commerce_rpc.create_order(899801,'access-funded',rows,source)->>'order_id')::uuid;
    invoiced:=(commerce_rpc.create_order(899801,'access-invoiced',rows,source)->>'order_id')::uuid;
    perform commerce_rpc.decide_partner_sheet(9,approved,true,price,'access-approve');
    perform commerce_rpc.decide_partner_sheet(9,funded,true,price,'access-fund-approve');
    perform commerce_rpc.decide_partner_sheet(9,invoiced,true,price,'access-invoice-approve');
    select (pricing_snapshot->>'policy_version')::bigint into policy from commerce.orders where id=funded;
    perform commerce_rpc.checkout_consent(funded,899801,policy,'access-consent');
    perform commerce_rpc.confirm_receipt(899801,'access-funding','fixture','access-funding',3000,'fixture-proof');
    assert commerce_rpc.reserve_order(funded,899801,'access-consent')->>'state'='held';
    -- Existing issued invoices are also commitments and remain payable.
    insert into commerce.payment_requests(id,user_id,order_id,provider,receiver,quote)
        select gen_random_uuid(),user_id,id,'cryptopay','fixture','{}' from commerce.orders where id=invoiced;
    select to_jsonb(o) into before_order from commerce.orders o where id=funded;
    before_journals:=(select count(*) from commerce.journals);
    begin perform commerce_rpc.configure_partner_sheet_access(899801,pid,false,'forged');
        raise exception 'Partner disabled their own permission';exception when insufficient_privilege then null;end;
    result:=commerce_rpc.configure_partner_sheet_access(9,pid,false,'access-disable');
    assert result->>'enabled'='false' and result->>'effective'='false';
    assert commerce_rpc.partner_sheet_context(899801)->>'is_partner'='true';
    assert commerce_rpc.partner_sheet_context(899801)->>'partner_sheet_enabled'='false';
    assert commerce_rpc.partner_sheet_context(899802)->>'partner_sheet_enabled'='true';
    assert (select to_jsonb(p)=before_terms from commerce.partners p where id=pid),'Access change altered partnership terms';
    assert (select to_jsonb(o)=before_order from commerce.orders o where id=funded),'Access change repriced funded order';
    assert (select count(*) from commerce.journals)=before_journals,'Permission change moved money';
    assert commerce_rpc.checkout_state(approved,899801)->>'partner_sheet_access_blocked'='true';
    assert commerce_rpc.checkout_state(funded,899801)->>'partner_sheet_access_blocked'='false';
    assert commerce_rpc.checkout_state(invoiced,899801)->>'partner_sheet_access_blocked'='false';
    perform commerce.require_approved_partner_sheet(funded);perform commerce.require_approved_partner_sheet(invoiced);
    begin perform commerce_rpc.decide_partner_sheet(9,pending,true,price,'access-forged-approval');
        raise exception 'Disabled pending sheet approved';exception when insufficient_privilege then null;end;
    begin perform commerce.require_approved_partner_sheet(approved);
        raise exception 'Disabled unpaid sheet can fund';exception when insufficient_privilege then null;end;
    select (pricing_snapshot->>'policy_version')::bigint into policy from commerce.orders where id=approved;
    perform commerce_rpc.checkout_consent(approved,899801,policy,'blocked-consent');
    begin perform commerce_rpc.reserve_order(approved,899801,'blocked-consent');
        raise exception 'Old button reserved disabled sheet';exception when insufficient_privilege then null;end;
    begin
        insert into commerce.payment_requests(id,user_id,order_id,provider,receiver,quote)
            select gen_random_uuid(),user_id,id,'cryptopay','fixture','{}' from commerce.orders where id=approved;
        raise exception 'Old button issued a new disabled-price invoice';exception when insufficient_privilege then null;end;
    assert commerce_rpc.reserve_order(funded,899801,'access-consent')->>'state'='held';
    perform commerce_rpc.bind_checker_job(funded,'disabled-access-existing-funded-job');
    ordinary:=(commerce_rpc.create_order(899801,'access-disabled-ordinary',rows,source)->>'order_id')::uuid;
    assert not exists(select 1 from commerce.partner_sheet_requests where order_id=ordinary);
    assert (select quoted_minor>0 and pricing_snapshot->>'partner_cost_sheet' is null from commerce.orders where id=ordinary);
    enabled_order:=(commerce_rpc.create_order(899802,'other-still-enabled',rows,source)->>'order_id')::uuid;
    assert exists(select 1 from commerce.partner_sheet_requests where order_id=enabled_order);
    assert (commerce_rpc.create_order(899801,'access-funded',rows,source)->>'order_id')::uuid=funded;
    result:=commerce_rpc.configure_partner_sheet_price(9,1600,'access-next-price');
    assert result->>'recipients'='1','Disabled partner received price notice';
    assert not exists(select 1 from commerce.partner_sheet_notifications where event_key='price:'||(result->>'version') and recipient_telegram_id=899801);
    perform commerce_rpc.configure_partner_sheet_access(21,pid,true,'access-enable');
    -- A delivery replay cannot undo a later enable decision.
    assert commerce_rpc.configure_partner_sheet_access(9,pid,false,'access-disable')->>'enabled'='true';
    assert (select count(*) from commerce.partner_sheet_access_settings where partner_id=pid)=2;
    enabled_order:=(commerce_rpc.create_order(899801,'access-enabled-again',rows,source)->>'order_id')::uuid;
    assert exists(select 1 from commerce.partner_sheet_requests where order_id=enabled_order);
    assert not exists(select 1 from commerce.partner_sheet_requests where order_id=ordinary),'Enable repriced a frozen ordinary quote';
    begin perform commerce_rpc.configure_partner_sheet_access(9,other,false,'access-disable');
        raise exception 'Conflicting replay changed another partner';exception when invalid_parameter_value then null;end;
    assert not has_function_privilege('anon','commerce_rpc.configure_partner_sheet_access(bigint,uuid,boolean,text)','EXECUTE');
    assert not has_function_privilege('authenticated','commerce_rpc.partner_sheet_access(bigint,uuid)','EXECUTE');
    assert not has_function_privilege('service_role','commerce.partner_sheet_access_enabled(uuid)','EXECUTE');
    raise notice 'PASS: per-partner access, defaults, ordinary pricing, stale-button denial, funded/invoiced preservation, audit replay and role isolation';
end $$;
rollback;
