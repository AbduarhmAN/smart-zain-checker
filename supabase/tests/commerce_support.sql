\set ON_ERROR_STOP on
begin;
do $$ declare o uuid; r jsonb; msg jsonb; edited jsonb; staff jsonb; receipt jsonb; original_hash text;
    first_key text:='aabbddee-1234-4321-8888-001122334455';edit_key text:='bbbbbbbb-1234-4321-8888-001122334455';
    reply_key text:='cccccccc-1234-4321-8888-001122334455';new_key text:='dddddddd-1234-4321-8888-001122334455';
begin
    perform commerce_rpc.configure_support_actors('[889799]','synthetic-local-admin-authority');
    perform commerce_rpc.register_telegram_user(889701,'synthetic-support');
    o:=(commerce_rpc.create_order(889701,'support-local-order','[{"row_number":2,"check_type":"service"}]',
        jsonb_build_object('file_sha256',repeat('a',64)))->>'order_id')::uuid;
    if commerce_rpc.support_order(o,889701,'customer')->>'owner_telegram_id'<>'889701' then raise exception 'Owner identity differs'; end if;
    begin perform commerce_rpc.support_order(o,889702,'customer');raise exception 'Cross-owner support allowed';exception when invalid_parameter_value then null;end;
    begin perform commerce_rpc.support_order(o,889701,'admin');raise exception 'Customer claimed staff role';exception when invalid_parameter_value then null;end;
    msg:='{"kind":"customer_message","body":"أحتاج مراجعة نتيجة الطلب","attachments":[{"kind":"document","file_id":"synthetic-file","file_unique_id":"synthetic-u","file_size":123,"file_name":"evidence.xlsx"}],"source":{"bot_id":77,"chat_id":889701,"message_id":101,"date":1700000000}}';
    r:=commerce_rpc.record_support_message(o,889701,'customer',first_key,msg);
    if r->>'state'<>'stored' or length(r->>'sha256')<>64 then raise exception 'Support evidence missing';end if;
    original_hash:=r->>'sha256';
    if commerce_rpc.record_support_message(o,889701,'customer',first_key,msg)->>'replayed'<>'true' then raise exception 'Message replay duplicated';end if;
    begin perform commerce_rpc.record_support_message(o,889701,'customer',first_key,jsonb_set(msg,'{body}','"changed"'));raise exception 'Changed replay accepted';exception when invalid_parameter_value then null;end;
    edited:=jsonb_set(jsonb_set(msg,'{kind}','"customer_edit"'),'{body}','"تصحيح: أحتاج مراجعة سطر واحد"');
    edited:=jsonb_set(edited,'{source,edit_date}','1700000100');
    perform commerce_rpc.record_support_message(o,889701,'customer',edit_key,edited);
    if (select record->>'body' from commerce.support_messages where event_key=first_key)<>'أحتاج مراجعة نتيجة الطلب'
        or (select evidence_sha256 from commerce.support_messages where event_key=first_key)<>original_hash then raise exception 'Edit overwrote original';end if;
    staff:='{"kind":"staff_reply","body":"راجعنا رسالتك وسنتحقق من السطر","attachments":[],"source":{"bot_id":77,"chat_id":889799,"message_id":201,"date":1700000200}}';
    perform commerce_rpc.record_support_message(o,889799,'admin',reply_key,staff);
    r:=commerce_rpc.support_history(o,889701,'customer');
    if jsonb_array_length(r->'messages')<>2 or exists(select 1 from jsonb_array_elements(r->'messages') m where m->>'kind'='staff_reply') then raise exception 'Draft leaked to customer';end if;
    if exists(select 1 from jsonb_array_elements(r->'messages') m where m->'source'<>'null'::jsonb or m->'attachments'->0 ? 'file_id') then raise exception 'Private metadata leaked';end if;
    receipt:=jsonb_build_object('chat',jsonb_build_object('id',889701),'from',jsonb_build_object('id',77,'is_bot',true),
        'message_id',301,'date',1700000300,'text','دعم الطلب: '||o::text||E'\n\n'||(staff->>'body'));
    begin perform commerce_rpc.confirm_support_delivery(reply_key,889701,receipt);raise exception 'Customer confirmed staff delivery';exception when invalid_parameter_value then null;end;
    begin perform commerce_rpc.confirm_support_delivery(reply_key,889799,jsonb_set(receipt,'{chat,id}','889702'));raise exception 'Wrong recipient accepted';exception when invalid_parameter_value then null;end;
    perform commerce_rpc.confirm_support_delivery(reply_key,889799,receipt);
    if commerce_rpc.confirm_support_delivery(reply_key,889799,receipt)->>'replayed'<>'true' then raise exception 'Delivery replay duplicated';end if;
    r:=commerce_rpc.support_history(o,889701,'customer');
    if jsonb_array_length(r->'messages')<>3 or not exists(select 1 from jsonb_array_elements(r->'messages') m where m->>'kind'='staff_reply' and m->>'delivery_state'='confirmed') then raise exception 'Confirmed reply missing';end if;
    r:=commerce_rpc.support_inbox(889799);
    if jsonb_array_length(r)<>1 or r->0->>'needs_reply'<>'false' then raise exception 'Inbox counts wrong';end if;
    perform commerce_rpc.set_support_state(o,889799,'closed','synthetic-close-once');
    perform commerce_rpc.record_support_message(o,889701,'customer',new_key,jsonb_set(msg,'{source,message_id}','102'));
    if commerce_rpc.set_support_state(o,889799,'closed','synthetic-close-once')->>'state'<>'open' then raise exception 'Old closure replay lost reopened customer issue';end if;
    begin perform commerce_rpc.set_support_state(o,889799,'open','synthetic-close-once');raise exception 'Conflicting state replay accepted';exception when invalid_parameter_value then null;end;
    if (select count(*) from commerce.support_messages where order_id=o)<>4 then raise exception 'Evidence duplicated or dropped';end if;
    if exists(select 1 from commerce.journals where order_id=o) then raise exception 'Support modified money';end if;
    perform commerce_rpc.configure_support_actors('[889798]','synthetic-authority-rotation');
    begin perform commerce_rpc.support_history(o,889799,'admin');raise exception 'Revoked staff retained access';exception when invalid_parameter_value then null;end;
    raise notice 'PASS: support ownership/admin authority, immutable source/edits, draft privacy, delivery proof/replay, closure replay, no financial effects';
end $$;
rollback;
