"""Admission against existing kernel memory boundaries, without guessed quotas."""
import os
from pathlib import Path


def memory_snapshot():
    result={'current':None,'high':None,'max':None,'available':None}
    group=Path('/sys/fs/cgroup')
    try:
        # Locate this process' cgroup, never measure an unrelated service.
        line=next(line for line in Path('/proc/self/cgroup').read_text().splitlines() if line.startswith('0::'))
        group=group/line[3:].lstrip('/')
    except (OSError,StopIteration): pass
    for key in ('current','high','max'):
        try:
            value=(group/('memory.'+key)).read_text().strip()
            result[key]=None if value=='max' else int(value)
        except (OSError,ValueError): pass
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith('MemAvailable:'):result['available']=int(line.split()[1])*1024
    except (OSError,ValueError): pass
    return result


def admit(role):
    state=memory_snapshot()
    reserve=int(os.environ.get('ZAIN_MEMORY_RESERVE_BYTES','0'))
    estimate=int(os.environ.get('ZAIN_'+role.upper()+'_TASK_MEMORY_BYTES','0'))
    if reserve<0 or estimate<0: raise ValueError('Memory admission values must be nonnegative')
    for limit in (state['high'],state['max']):
        if limit is not None and state['current'] is not None and state['current']+estimate>=limit: return False
    if state['available'] is not None and state['available']<reserve+estimate: return False
    return True
