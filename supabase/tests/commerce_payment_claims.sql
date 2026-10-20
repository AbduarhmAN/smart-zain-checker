\set ON_ERROR_STOP on
begin;
set local timezone='UTC';
do $$ declare q uuid:=gen_random_uuid(); q2 uuid:=gen_random_uuid(); q3 uuid:=gen_random_uuid();
    snap jsonb; ev jsonb; c uuid; c2 uuid; r jsonb; payment uuid;
begin
    snap:=jsonb_build_object('id',q,'user_id',889001,'amount_minor',3000,'asset','USDT','asset_amount','0.6',
        'egp_per_asset','50','rate_source','fixture','precision',6,'order_id',null,
        'observed_at',to_char(now()-interval '1 minute','YYYY-MM-DD"T"HH24:MI:SS')||'Z',
        'expires_at',to_char(now()+interval '14 minutes','YYYY-MM-DD"T"HH24:MI:SS')||'Z');
    perform commerce_rpc.create_payment_request(snap,'binance_pay','pay:42',
        '{"pay_id":42,"uid":null,"asset":"USDT"}');
    c:=(commerce_rpc.submit_payment_claim(q,889001,'ORDER1')->>'claim_id')::uuid;
    if (commerce_rpc.submit_payment_claim(q,889001,'ORDER1')->>'claim_id')::uuid<>c then
        raise exception 'Claim replay created another review'; end if;
    if (commerce_rpc.wallet_state(889001)->'customer'->>'available_minor')::numeric<>0 then
        raise exception 'Submitting a reference credited money'; end if;
    begin
        perform commerce_rpc.submit_payment_claim(q,889002,'ORDER1');
        raise exception 'Another user claimed the quote';
    exception when invalid_parameter_value then null; end;
    begin
        perform commerce_rpc.submit_payment_claim(q,889001,'TX1');
        raise exception 'Second pending claim allowed for one quote';
    exception when invalid_parameter_value then null; end;
    ev:=jsonb_build_object('provider','binance_pay','reference','TX1','asset','USDT','amount','0.6','receiver','pay:42',
        'received_at',to_char(now(),'YYYY-MM-DD"T"HH24:MI:SS')||'Z','invoice_payload',null,
        'proof_json','{"transaction_id":"TX1","order_id":"ORDER1"}');
    r:=commerce_rpc.approve_payment_claim(c,889900,ev);payment:=(r->>'payment_id')::uuid;
    if (commerce_rpc.approve_payment_claim(c,889901,ev)->>'payment_id')::uuid<>payment then
        raise exception 'Approval replay produced another payment'; end if;
    if (commerce_rpc.payment_claim(c)->>'actor_telegram_id')::bigint<>889900 then
        raise exception 'Replay replaced the original assignment audit'; end if;
    if (commerce_rpc.wallet_state(889001)->'customer'->>'available_minor')::numeric<>3000 then
        raise exception 'Approval did not credit exactly once'; end if;
    begin
        perform commerce_rpc.reject_payment_claim(c,889900,'after-paid');
        raise exception 'Approved money was rejected without reversal';
    exception when invalid_parameter_value then null; end;
    perform commerce_rpc.create_payment_request(snap||jsonb_build_object('id',q2,'user_id',889002),'binance_pay','pay:42');
    c2:=(commerce_rpc.submit_payment_claim(q2,889002,'TX1')->>'claim_id')::uuid;
    begin
        perform commerce_rpc.approve_payment_claim(c2,889900,ev);
        raise exception 'Canonical transfer credited another customer';
    exception when invalid_parameter_value then null; end;
    if (commerce_rpc.wallet_state(889002)->'customer'->>'available_minor')::numeric<>0
       or commerce_rpc.payment_claim(c2)->>'state'<>'pending' then
        raise exception 'Failed approval was not atomic'; end if;
    perform commerce_rpc.reject_payment_claim(c2,889900,'review-reject:1');
    perform commerce_rpc.reject_payment_claim(c2,889901,'review-reject:2');
    if (commerce_rpc.payment_claim(c2)->>'actor_telegram_id')::bigint<>889900 then
        raise exception 'Rejection audit was replaced'; end if;

    perform commerce_rpc.create_payment_request(snap||jsonb_build_object('id',q3,'user_id',889003),'bsc',
        '0x'||repeat('c',40),jsonb_build_object('chain_id',56,'asset','USDT','contract','0x'||repeat('d',40),'decimals',18));
    c2:=(commerce_rpc.submit_payment_claim(q3,889003,'0x'||repeat('a',64))->>'claim_id')::uuid;
    ev:=ev||jsonb_build_object('provider','bsc','reference','56:0x'||repeat('a',64),'receiver','0x'||repeat('c',40),
        'proof_json',jsonb_build_object('chain_id',56,'tx_hash','0x'||repeat('a',64),
            'contract','0x'||repeat('e',40),'decimals',18)::text);
    begin
        perform commerce_rpc.approve_payment_claim(c2,889900,ev);
        raise exception 'Wrong token contract matched the stored quote';
    exception when invalid_parameter_value then null; end;
    ev:=ev||jsonb_build_object('proof_json',jsonb_build_object('chain_id',56,'tx_hash','0x'||repeat('a',64),
        'contract','0x'||repeat('d',40),'decimals',18)::text);
    perform commerce_rpc.approve_payment_claim(c2,889900,ev);
    if (commerce_rpc.wallet_state(889003)->'customer'->>'available_minor')::numeric<>3000 then
        raise exception 'Approved token transfer did not credit its owner'; end if;
    if has_function_privilege('anon','commerce_rpc.approve_payment_claim(uuid,bigint,jsonb)','EXECUTE')
       or has_function_privilege('authenticated','commerce_rpc.list_payment_claims(integer,integer)','EXECUTE')
       or has_function_privilege('service_role','commerce.create_payment_request(jsonb,text,text)','EXECUTE') then
        raise exception 'Manual payment trust boundary is exposed'; end if;
end $$;
rollback;
