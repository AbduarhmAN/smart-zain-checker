-- Synthetic transactional acceptance. Run only in the isolated test database.
begin;
do $$
declare
    partner uuid;other_partner uuid;old_offer jsonb;pending jsonb;declined jsonb;share_changed jsonb;share_increased jsonb;limits_changed jsonb;ended jsonb;
    rules jsonb;policy jsonb;setting jsonb;result jsonb;record jsonb;old_campaign jsonb;old_order jsonb;order_id uuid;
    blocked integer:=0;notices bigint;before_ledger bigint;notice jsonb;old_notice_seen boolean:=false;request_notice_seen boolean:=false;
begin
    perform commerce_rpc.configure_support_actors('[9,21]'::jsonb,'campaign-approval-fixture-admins');
    partner:=(commerce_rpc.create_partner(41,'REVIEWPARTNER')->>'partner_id')::uuid;
    other_partner:=(commerce_rpc.create_partner(51,'NOAPPROVAL')->>'partner_id')::uuid;
    perform commerce_rpc.configure_partner(partner,9,3000,'forever',null,true);
    perform commerce_rpc.configure_partner(other_partner,9,3000,'forever',null,true);
    policy:=jsonb_build_object('max_percent_basis_points',5000,'max_checks_per_user',4000,
        'max_discount_minor_per_user',100000,'max_total_discount_minor',1000000,
        'max_duration_seconds',259200,'max_wallet_credit_minor',100000,'max_redemptions',1000);
    perform commerce_rpc.configure_campaign_policy(9,policy,'campaign-approval-fixture-limits');
    rules:=jsonb_build_object('benefit_kind','percent','benefit_value',2000,'eligibility','any',
        'check_types',jsonb_build_array('account','service'),'max_checks',10,'max_discount_minor',1000,
        'max_total_discount_minor',10000,'duration_seconds',86400,'max_redemptions',100);
    select count(*) into before_ledger from commerce.journals;

    setting:=commerce_rpc.partner_campaign_approval_setting(9,41);
    assert setting->>'required'='false','Approval must default off';
    old_offer:=commerce_rpc.create_partner_campaign(41,'old-offer','OLDAPPROVED',rules,'fixture-original-native-review');
    assert old_offer->>'approval_state'='not_required';
    select to_jsonb(c) into old_campaign from commerce.campaigns c where id=(old_offer->>'campaign_id')::uuid;
    assert commerce_rpc.activate_campaign(31,'OLDAPPROVED','fixture-old-activation')->>'state'='activated';
    order_id:=(commerce_rpc.create_order(31,'frozen-before-review','[{"row_number":2,"check_type":"account"}]',
        '{"file_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}')->>'order_id')::uuid;
    select to_jsonb(o) into old_order from commerce.orders o where id=order_id;

    setting:=commerce_rpc.configure_partner_campaign_approval(9,41,true,'enable-review-1');
    assert setting->>'required'='true' and setting->>'reviewer_telegram_id'='9';
    assert commerce_rpc.partner_campaign_context(41)->>'approval_required'='true';
    assert commerce_rpc.partner_campaign_context(51)->>'approval_required'='false';
    pending:=commerce_rpc.create_partner_campaign(41,'needs-review','WAITREVIEW',rules,'fixture-partner-reviewed-rules');
    assert pending->>'approval_state'='pending' and pending->>'state'='draft';
    assert (select not active from commerce.codes where campaign_id=(pending->>'campaign_id')::uuid);
    assert commerce_rpc.activate_campaign(32,'WAITREVIEW','fixture-pending-customer')->>'state'='inactive_offer';
    assert not exists(select 1 from commerce.campaign_usage where campaign_id=(pending->>'campaign_id')::uuid);
    record:=commerce_rpc.partner_campaign_approval_record(9,(pending->>'campaign_id')::uuid);
    assert record->'rules'=rules and record->>'basis_points'='3000' and record->>'code'='WAITREVIEW';
    assert record->>'partner_telegram_id'='41' and record->>'reviewer_telegram_id'='9';
    assert jsonb_array_length(commerce_rpc.list_partner_campaign_approvals(9))=1;
    assert commerce_rpc.list_partner_campaign_approvals(21)='[]'::jsonb;

    begin perform commerce_rpc.decide_partner_campaign(21,(pending->>'campaign_id')::uuid,true,'wrong-reviewer');
    exception when insufficient_privilege then blocked:=blocked+1;end;
    begin perform commerce_rpc.partner_campaign_approval_record(21,(pending->>'campaign_id')::uuid);
    exception when insufficient_privilege then blocked:=blocked+1;end;
    begin perform commerce_rpc.configure_partner_campaign_approval(99,41,true,'not-admin');
    exception when insufficient_privilege then blocked:=blocked+1;end;
    begin perform commerce_rpc.pause_partner_campaign(41,(pending->>'campaign_id')::uuid,'pending-pause');
    exception when invalid_parameter_value then blocked:=blocked+1;end;
    begin update commerce.codes set active=true where campaign_id=(pending->>'campaign_id')::uuid;
    exception when insufficient_privilege then blocked:=blocked+1;end;
    begin update commerce.campaigns set state='active',approved_at=now() where id=(pending->>'campaign_id')::uuid;
    exception when insufficient_privilege then blocked:=blocked+1;end;
    assert blocked=6,'Pending campaign authority or activation bypass';

    perform commerce_rpc.configure_partner_campaign_approval(9,41,false,'disable-review-1');
    assert commerce_rpc.create_partner_campaign(41,'needs-review','WAITREVIEW',rules,'retry-after-toggle')->>'approval_state'='pending';
    result:=commerce_rpc.create_partner_campaign(41,'new-after-disable','OFFACTIVE',rules,'fixture-off-native-review');
    assert result->>'approval_state'='not_required' and result->>'state'='active';
    perform commerce_rpc.configure_partner_campaign_approval(9,41,true,'enable-review-1');
    assert commerce_rpc.partner_campaign_approval_setting(9,41)->>'required'='false','Stale setting replay changed current option';
    assert (select to_jsonb(c)=old_campaign from commerce.campaigns c where id=(old_offer->>'campaign_id')::uuid);
    assert (select to_jsonb(o)=old_order from commerce.orders o where id=order_id),'Saved order changed';

    result:=commerce_rpc.decide_partner_campaign(9,(pending->>'campaign_id')::uuid,true,'approve-original');
    assert result->>'approval_state'='approved';
    assert commerce_rpc.activate_campaign(32,'WAITREVIEW','fixture-approved-customer')->>'state'='activated';
    assert commerce_rpc.decide_partner_campaign(9,(pending->>'campaign_id')::uuid,true,'approve-original')->>'replayed'='true';
    select count(*) into notices from commerce.partner_sheet_notifications where event_key='campaign-decision:'||(pending->>'campaign_id');
    assert notices=1;
    begin perform commerce_rpc.decide_partner_campaign(9,(pending->>'campaign_id')::uuid,false,'opposite-decision');
    exception when invalid_parameter_value then blocked:=blocked+1;end;
    perform commerce_rpc.pause_partner_campaign(41,(pending->>'campaign_id')::uuid,'pause-approved');
    perform commerce_rpc.decide_partner_campaign(9,(pending->>'campaign_id')::uuid,true,'approve-original');
    assert (select state='paused' from commerce.campaigns where id=(pending->>'campaign_id')::uuid),'Replay reactivated paused code';

    perform commerce_rpc.configure_partner_campaign_approval(9,41,true,'enable-review-2');
    declined:=commerce_rpc.create_partner_campaign(41,'decline-me','DECLINEME',rules,'fixture-decline-rules');
    result:=commerce_rpc.decide_partner_campaign(9,(declined->>'campaign_id')::uuid,false,'decline-original');
    assert result->>'state'='rejected';
    assert (select state='ended' from commerce.campaigns where id=(declined->>'campaign_id')::uuid);
    assert (select not active from commerce.codes where campaign_id=(declined->>'campaign_id')::uuid);
    assert commerce_rpc.create_partner_campaign(41,'decline-me','DECLINEME',rules,'stale-create')->>'approval_state'='rejected';
    begin update commerce.codes set active=true where campaign_id=(declined->>'campaign_id')::uuid;
    exception when insufficient_privilege then blocked:=blocked+1;end;
    begin perform commerce_rpc.decide_partner_campaign(9,(declined->>'campaign_id')::uuid,true,'revive-declined');
    exception when invalid_parameter_value then blocked:=blocked+1;end;

    share_changed:=commerce_rpc.create_partner_campaign(41,'share-changes','SHARECHANGES',rules,'fixture-share');
    perform commerce_rpc.configure_partner(partner,9,1000,'forever',null,true);
    begin perform commerce_rpc.decide_partner_campaign(9,(share_changed->>'campaign_id')::uuid,true,'share-too-low');
    exception when invalid_parameter_value then blocked:=blocked+1;end;
    assert commerce_rpc.partner_campaign_approval_record(9,(share_changed->>'campaign_id')::uuid)->>'state'='pending';
    perform commerce_rpc.configure_partner(partner,9,3000,'forever',null,true);
    share_increased:=commerce_rpc.create_partner_campaign(41,'share-increases','SHAREINCREASES',rules,'fixture-share-increase');
    perform commerce_rpc.configure_partner(partner,9,4000,'forever',null,true);
    begin perform commerce_rpc.decide_partner_campaign(9,(share_increased->>'campaign_id')::uuid,true,'share-increased');
    exception when invalid_parameter_value then blocked:=blocked+1;end;
    assert commerce_rpc.partner_campaign_approval_record(9,(share_increased->>'campaign_id')::uuid)->>'state'='pending';
    perform commerce_rpc.configure_partner(partner,9,3000,'forever',null,true);
    limits_changed:=commerce_rpc.create_partner_campaign(41,'limits-change','LIMITSCHANGES',rules,'fixture-limits');
    perform commerce_rpc.configure_campaign_policy(9,policy||'{"max_checks_per_user":1}'::jsonb,'tighten-limits');
    begin perform commerce_rpc.decide_partner_campaign(9,(limits_changed->>'campaign_id')::uuid,true,'limits-too-low');
    exception when invalid_parameter_value then blocked:=blocked+1;end;
    perform commerce_rpc.configure_campaign_policy(9,policy,'restore-limits');
    ended:=commerce_rpc.create_partner_campaign(41,'partner-ends','PARTNERENDS',rules,'fixture-end');
    perform commerce_rpc.configure_partner(partner,9,3000,'forever',null,false);
    begin perform commerce_rpc.decide_partner_campaign(9,(ended->>'campaign_id')::uuid,true,'ended-partner');
    exception when invalid_parameter_value then blocked:=blocked+1;end;
    perform commerce_rpc.decide_partner_campaign(9,(ended->>'campaign_id')::uuid,false,'reject-ended-partner');
    assert blocked=13,'Expected authority/share/limit/final-decision guards did not all fire';
    assert (select count(*)=before_ledger from commerce.journals),'Review changed money';
    assert (select count(*)=1 from commerce.partner_sheet_notifications
        where event_key='campaign-request:'||(pending->>'campaign_id') and recipient_telegram_id=9
            and payload->>'kind'='campaign_request');
    assert has_function_privilege('service_role','commerce_rpc.decide_partner_campaign(bigint,uuid,boolean,text)','execute');
    assert not has_function_privilege('anon','commerce_rpc.decide_partner_campaign(bigint,uuid,boolean,text)','execute');
    assert not has_function_privilege('authenticated','commerce_rpc.decide_partner_campaign(bigint,uuid,boolean,text)','execute');
    assert not has_table_privilege('service_role','commerce.partner_campaign_approvals','update');
    insert into commerce.partner_sheet_notifications(event_key,recipient_telegram_id,payload)
        values('fixture-old-price-notice',9,'{"kind":"price","version":1,"amount_minor":0.001}'::jsonb);
    loop
        notice:=commerce_rpc.claim_partner_sheet_notification();
        exit when notice is null;
        if notice->'payload'->>'kind'='campaign_request' then
            assert notice->'payload'->'approval_record'->>'campaign_id'=notice->'payload'->>'campaign_id';
            assert notice->'payload'->'approval_record'->'rules'=rules;
            assert notice->'payload'->'approval_record'->>'basis_points'='3000';
            request_notice_seen:=true;
        elsif notice->>'event_key'='fixture-old-price-notice' then
            assert notice->'payload'='{"kind":"price","version":1,"amount_minor":0.001}'::jsonb;
            old_notice_seen:=true;
        end if;
        perform commerce_rpc.complete_partner_sheet_notification((notice->>'id')::uuid,(notice->>'token')::uuid,'rejected',null);
    end loop;
    assert request_notice_seen and old_notice_seen,'Notification claim must embed review and preserve old notice payloads';
    raise notice 'Partner campaign approval acceptance passed: reviewer authority, atomic inactive issue, immutable replay, frozen prior data, limits/share recheck, exact-money no-op.';
end $$;
rollback;
