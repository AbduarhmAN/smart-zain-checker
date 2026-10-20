-- Run only through the management API with read_only=true.
select jsonb_build_object('observed_at',now(),'complete',true,'orders',
    coalesce((select jsonb_agg(jsonb_build_object(
        'order_id',o.id,'input_sha256',o.source_snapshot->>'file_sha256','result_sha256',o.result_sha256,
        'execution_state',o.execution_state,'delivery_state',o.delivery_state,'payment_state',o.payment_state,
        'financial_completed_at',o.financial_completed_at,'final_charge_minor',o.final_charge_minor,
        'archives',coalesce((select jsonb_agg(jsonb_build_object('kind',a.kind,'state',a.state,
            'sha256',a.sha256,'file_id',a.telegram_file_id,'message_id',a.telegram_message_id,
            'file_size',a.file_size,'retention_until',a.retention_until))
            from commerce.archive_files a where a.order_id=o.id),'[]'::jsonb)) order by o.id)
        from commerce.orders o),'[]'::jsonb)) as snapshot;
