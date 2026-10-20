// Execute the real request function against a stub transport; no browser or network.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('chrome_extension/service-worker.js','utf8');
const begin = source.indexOf('async function fetchJson(');
const end = source.indexOf('\nasync function isTargetTab(',begin);
assert.ok(begin>=0 && end>begin);
const calls=[];
const ctx=vm.createContext({URL,Headers,JSON,Error,
  ZAIN_BRIDGE_WORKER_ID:'worker_3',BRIDGE_TOKEN:'a'.repeat(64),BRIDGE_URL:'http://127.0.0.1:8766',
  safeStorageGet:async()=>({zainWorkerId:'worker_9'}),
  fetch:async(url,options)=>{calls.push({url,options});return {ok:true,json:async()=>({status:'wait'})};}});
vm.runInContext(source.slice(begin,end),ctx);
async function main(){
  await ctx.fetchJson('http://127.0.0.1:8766/task?worker=worker_9',{worker_id:'worker_8'});
  assert.equal(new URL(calls[0].url).searchParams.get('worker'),'worker_3');
  assert.equal(calls[0].options.headers.get('X-Worker-Id'),'worker_3');
  await ctx.fetchJson('http://127.0.0.1:8766/result',{method:'POST',
    headers:{'content-type':'application/json','x-zain-bridge-token':'wrong','x-worker-id':'worker_8'},
    body:JSON.stringify({worker_id:'worker_9',task_id:'fixture',amount:0})});
  assert.equal(calls[1].options.headers.get('X-Zain-Bridge-Token'),'a'.repeat(64));
  assert.equal(JSON.parse(calls[1].options.body).worker_id,'worker_3');
  assert.equal(JSON.parse(calls[1].options.body).amount,0);
  const before=calls.length;
  await assert.rejects(ctx.fetchJson('https://external.example/task'));
  await assert.rejects(ctx.fetchJson('http://127.0.0.1:8766/result',{body:'[]'}));
  assert.equal(calls.length,before);
  console.log(JSON.stringify({passed:true,checks:6,network:'stubbed',managed_worker_pinned:true}));
}
main().catch(error=>{console.error(error);process.exitCode=1;});
