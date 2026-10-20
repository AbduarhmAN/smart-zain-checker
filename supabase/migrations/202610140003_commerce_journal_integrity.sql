-- A06: an append-only balanced journal; all lines commit with the journal.
begin;
create function commerce.enforce_balanced_journal()
returns trigger language plpgsql set search_path = '' as $$
declare v_id uuid; v_count bigint; v_total numeric;
begin
    if TG_TABLE_NAME = 'journals' then v_id := NEW.id;
    else v_id := NEW.journal_id; end if;
    select count(*),coalesce(sum(delta_minor::numeric),0)
    into v_count,v_total from commerce.ledger_entries where journal_id = v_id;
    if v_count < 2 or v_total <> 0 then
        raise exception 'Journal must contain at least two balanced lines' using errcode = '23514';
    end if;
    return null;
end $$;
create constraint trigger journal_must_balance after insert on commerce.journals
deferrable initially deferred for each row execute function commerce.enforce_balanced_journal();
create constraint trigger journal_lines_must_balance after insert on commerce.ledger_entries
deferrable initially deferred for each row execute function commerce.enforce_balanced_journal();

create function commerce.prevent_financial_rewrite()
returns trigger language plpgsql set search_path = '' as $$
begin
    raise exception 'Financial journals and entries are append-only' using errcode = '23514';
end $$;
create trigger journal_immutable before update on commerce.journals
for each row execute function commerce.prevent_financial_rewrite();
create trigger ledger_immutable before update on commerce.ledger_entries
for each row execute function commerce.prevent_financial_rewrite();
revoke all on function commerce.enforce_balanced_journal(), commerce.prevent_financial_rewrite() from public,anon,authenticated,service_role;
commit;
