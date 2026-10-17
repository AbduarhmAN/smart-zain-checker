\set ON_ERROR_STOP on
begin;
do $$ declare j uuid; begin
    insert into commerce.journals(idempotency_key,kind,payload_fingerprint)
    values ('fixture-journal-balanced','adjustment','fixture') returning id into j;
    insert into commerce.ledger_entries(journal_id,line_number,account_key,delta_minor)
    values (j,1,'fixture-cash',200),(j,2,'fixture-clearing',-200);
    set constraints all immediate;
    set constraints all deferred;
    begin
        insert into commerce.journals(idempotency_key,kind,payload_fingerprint)
        values ('fixture-journal-balanced','adjustment','fixture');
        raise exception 'Duplicate financial operation accepted';
    exception when unique_violation then null; end;
    begin
        update commerce.ledger_entries set delta_minor = 300 where journal_id = j and line_number = 1;
        raise exception 'Financial entry rewrite accepted';
    exception when check_violation then null; end;
    begin
        insert into commerce.journals(idempotency_key,kind,payload_fingerprint)
        values ('fixture-journal-unbalanced','adjustment','fixture') returning id into j;
        insert into commerce.ledger_entries(journal_id,line_number,account_key,delta_minor)
        values (j,1,'fixture-cash',100),(j,2,'fixture-clearing',-50);
        set constraints all immediate;
        raise exception 'Unbalanced journal accepted';
    exception when check_violation then null; end;
    set constraints all deferred;
    if exists (select 1 from commerce.journals where idempotency_key = 'fixture-journal-unbalanced') then
        raise exception 'Failed journal was partially committed';
    end if;
    raise notice 'PASS: balanced journal, unique operation, immutable entry, rejected imbalance and atomic failure';
end $$;
commit;
