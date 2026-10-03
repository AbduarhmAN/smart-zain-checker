// Offline arithmetic and lifecycle checks. No live checker requests.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const nodes = new Map(), store = new Map();
const ctx = vm.createContext({Intl,Date,Number,Math,JSON,setInterval:()=>1,
  document:{getElementById(id){if(!nodes.has(id))nodes.set(id,{textContent:''});return nodes.get(id);}},
  localStorage:{getItem:key=>store.get(key),setItem:(key,value)=>store.set(key,value)}});
vm.runInContext(fs.readFileSync(new URL('../web_ui/static/js/session_timing.js',import.meta.url),'utf8'),ctx);
const timing = vm.runInContext('SessionTiming',ctx);
const at = 1700000000000;
const data = (done,extra={})=>({workbook:'sample.xlsx',running:true,paused:false,
  kpis:{total:100,completed:done},verifications:[],...extra});
const metadata = (now,id='first',start=at,initial=20)=>({
  session_id:id,started_at:start/1000,server_time:now/1000,initial_completed:initial,
  recent_completions:initial===20?Array.from({length:11},(_,i)=>({age_seconds:10-i,kind:'account'})):[]});
timing.update(data(40),at+20000);
assert.equal(timing.estimate(at+20000).status,'unknown_start','Page load must never become the checker start');
assert.equal(nodes.get('timingStart').textContent,'—');
assert.equal(nodes.get('timingFinish').textContent,'—');
timing.update(data(40,{session_timing:metadata(at+20000)}),at+25000);
let e=timing.estimate(at+25000);
assert.equal(e.startedAt,at,'Show the real start timestamp without replacing it with page load or clock offset');
assert.equal(e.elapsed,20);
assert.equal(e.measured,20,'Resume counts must exclude already completed rows');
assert.equal(e.seconds,60);
assert.equal(e.endsAt,at+85000);
assert.equal((nodes.get('timingStart').textContent.match(/:/g)||[]).length,2);
assert.equal((nodes.get('timingFinish').textContent.match(/:/g)||[]).length,1,'Only the finish clock hides seconds');
assert.equal(timing.duration(203),'٣ دقائق و٢٣ ثانية','Arabic durations must unambiguously show minutes before seconds');
assert.equal(timing.duration(61),'دقيقة واحدة وثانية واحدة');
// Reload after the file was already being checked: same start and same baseline.
timing.state=null;
timing.update(data(50,{session_timing:metadata(at+30000)}),at+35000);
assert.equal(timing.state.startedAt,at);
assert.equal(timing.estimate(at+35000).seconds,50);
timing.update(data(50,{paused:true,session_timing:metadata(at+31000)}),at+36000);
assert.equal(timing.estimate(at+36000).status,'paused');
timing.update(data(50,{verifications:[{id:'challenge'}],session_timing:metadata(at+32000)}),at+37000);
assert.equal(timing.estimate(at+37000).status,'paused');
timing.offline();assert.equal(timing.estimate(at+38000).status,'offline');
timing.update(data(60,{session_timing:metadata(at+40000)}),at+45000);
assert.equal(timing.estimate(at+55001).status,'offline');
timing.update(data(60,{session_timing:metadata(at+50000,'second',at+45000,60)}),at+55000);
assert.equal(timing.estimate(at+55000).status,'sampling');
assert.equal(timing.state.startedAt,at+45000,'A new session for the same file gets its own real start');
timing.update(data(100,{session_timing:metadata(at+60000,'second',at+45000,60)}),at+65000);
assert.equal(timing.estimate(at+65000).status,'saving');
timing.update(data(100,{running:false,session_timing:metadata(at+61000,'second',at+45000,60)}),at+66000);
const finishedElapsed=timing.estimate(at+66000).elapsed;
assert.equal(timing.estimate(at+66000).status,'finished');
timing.update(data(100,{running:false,session_timing:metadata(at+62000,'second',at+45000,60)}),at+67000);
assert.equal(timing.estimate(at+67000).elapsed,finishedElapsed);
timing.update(data(100,{_preview:true,running:false}),at+68000);
assert.equal(timing.estimate(at+68000).status,'idle');
timing.update(data(50,{running:false,session_timing:metadata(at+70000,'third',at+65000,45)}),at+75000);
assert.equal(timing.estimate(at+75000).status,'stopped');
timing.update(data(0,{kpis:{total:0,completed:0}}),at+76000);
assert.equal(timing.estimate(at+76000).status,'idle');

// Recent service performance replaces an optimistic lifetime average.
const recent=(now,done,extra={})=>data(done,{session_timing:{...metadata(now,'recent',at,0),
  recent_completions:Array.from({length:11},(_,i)=>({age_seconds:(10-i)*3,kind:'service'})),
  remaining_by_type:{account:0,service:100-done}},...extra});
timing.update(recent(at+100000,90),at+100000);
assert.equal(timing.estimate(at+100000).seconds,30);
assert.equal(timing.estimate(at+100000).sample,10);
// A transition to retries must not reuse regular-row speed.
timing.update(recent(at+101000,90,{kpis:{total:100,completed:90,deferred:10}}),at+101000);
assert.equal(timing.estimate(at+101000).status,'sampling');
timing.update(recent(at+110000,93,{kpis:{total:100,completed:93,deferred:7},session_timing:{
  ...metadata(at+110000,'recent',at,0),remaining_by_type:{account:0,service:7},
  recent_completions:[{age_seconds:8,kind:'service'},{age_seconds:4,kind:'service'},{age_seconds:0,kind:'service'}]}}),at+110000);
assert.equal(timing.estimate(at+110000).seconds,28);
assert.equal(timing.estimate(at+110000).phase,'retry');
timing.update(recent(at+111000,93,{kpis:{total:100,completed:93,deferred:7,active_leases:0,next_retry_seconds:420},
  workers:[{worker_id:'one',status:'cooldown',in_cooldown:true,cooldown_remaining_seconds:420}]}),at+111000);
assert.equal(timing.estimate(at+111000).status,'cooldown');
assert.equal(nodes.get('timingFinish').textContent,'—');
// Old runtimes use only the existing progress snapshots, not the entire results.
for(let i=0;i<200;i++){
  const legacy=data(i,{kpis:{total:1000,completed:i},session_timing:metadata(at+200000+i*1000,'legacy',at,0)});
  delete legacy.session_timing.recent_completions;
  Object.defineProperty(legacy,'records',{get(){throw new Error('Do not scan results for ETA');}});
  timing.update(legacy,at+200000+i*1000);
  assert.ok(timing.state.points.length<=11);
}
assert.equal(timing.estimate(at+399000).sample,10);
assert.equal(store.size,0,'No per-second localStorage writes');
console.log('PASS: real session start, no page-load fallback, resume baseline, clock offset, reload, pause, stale data, finish, and seconds hidden only on finish time.');
