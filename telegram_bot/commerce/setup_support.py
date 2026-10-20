"""Sync support administrators from the owner's local bot settings only."""
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4
from .client import CommerceClient


ROOT=Path(__file__).resolve().parents[2]


def main():
    from dotenv import load_dotenv
    load_dotenv(ROOT/'.env')
    from telegram_bot.config import settings
    actors=sorted({actor for actor in settings()['authorized_ids'] if type(actor) is int and 0<actor<2**52})
    if not 1<=len(actors)<=30:raise ValueError('Configure 1-30 local bot administrators first')
    reference='verified-local-bot-admins:sha256:'+hashlib.sha256(json.dumps(actors).encode()).hexdigest()
    result=CommerceClient().rpc('configure_support_actors',{'p_actor_ids':actors,'p_reference':reference})
    if not isinstance(result,dict) or result.get('state')!='configured' or result.get('active_count')!=len(actors):
        raise ValueError('Support authority confirmation differs from local selection')
    folder=ROOT/'.commerce';folder.mkdir(parents=True,exist_ok=True)
    path=folder/('support_authority_'+str(uuid4())+'.json')
    with path.open('x',encoding='utf-8') as stream:
        json.dump({'at':datetime.now(timezone.utc).isoformat(),'selected_actors':actors,'reference':reference,'receipt':result},stream,indent=2)
        stream.flush();os.fsync(stream.fileno())
    return {'state':'configured','active_count':result['active_count'],'private_receipt_saved':True}


if __name__=='__main__':print(json.dumps(main()))
