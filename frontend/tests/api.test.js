import test from 'node:test';
import assert from 'node:assert/strict';
import { request, ApiError, consumeEvents, escapeHTML, safeURL, mergeEvents, taskActions } from '../src/api.js';
import { taskDialog, chatPage, overview } from '../src/ui.js';

test('API authenticates via header and preserves caller idempotency key on retry',async()=>{
 const original=globalThis.fetch;const calls=[];
 globalThis.fetch=async(url,options)=>{calls.push({url,options});return Response.json({accepted:true});};
 try{for(let i=0;i<2;i++)await request('/tasks',{method:'POST',body:{title:'修复'},key:'stable-key'},'private-token');
 assert.equal(calls[0].url,'/api/v1/tasks');
 assert.equal(calls[0].options.headers.Authorization,'Bearer private-token');
 assert.equal(calls[0].options.headers['Idempotency-Key'],calls[1].options.headers['Idempotency-Key']);
 assert.ok(!calls[0].url.includes('private-token'));}finally{globalThis.fetch=original;}
});
test('API exposes validation and conflict errors without losing status',async()=>{
 const original=globalThis.fetch;
 try{globalThis.fetch=async()=>Response.json({detail:'answer unresolved attention first'},{status:409});
 await assert.rejects(request('/test'),error=>error instanceof ApiError&&error.status===409&&error.message.includes('attention'));
 globalThis.fetch=async()=>Response.json({detail:[{loc:['body','goal'],msg:'required'}]},{status:422});
 await assert.rejects(request('/test'),/goal：required/);}finally{globalThis.fetch=original;}
});
test('SSE reconstructs fragmented UTF-8, heartbeats, named events and cursor',async()=>{
 const raw=': heartbeat\r\n\r\nid: 7\r\nevent: arbitrary.kind\r\ndata: {"sequence":7,"content":"进展"}\r\n\r\nid: 8\ndata: {"sequence":8}\n\n';
 const bytes=new TextEncoder().encode(raw);const events=[];
 const body=new ReadableStream({start(controller){for(let i=0;i<bytes.length;i+=3)controller.enqueue(bytes.slice(i,i+3));controller.close();}});
 await consumeEvents(new Response(body,{headers:{'Content-Type':'text/event-stream'}}),(data,id)=>events.push([data,id]));
 assert.deepEqual(events,[[{sequence:7,content:'进展'},'7'],[{sequence:8},'8']]);
});
test('event replay deduplicates stable sequence and bounds display history',()=>{
 assert.deepEqual(mergeEvents([{sequence:1},{sequence:2}],[{sequence:2},{sequence:3}],2).map(e=>e.sequence),[2,3]);
});
test('untrusted GitHub, model and artifact text cannot inject HTML or javascript links',()=>{
 assert.ok(!escapeHTML('<img src=x onerror=alert(1)>').includes('<img'));
 assert.equal(safeURL('javascript:alert(1)'),'');assert.equal(safeURL('data:text/html,test'),'');
 assert.equal(safeURL('https://github.com/a/b'),'https://github.com/a/b');
 const t={id:'id',title:'<script>danger</script>',goal:'<img src=x>',status:'running',scope:'analysis',acceptance:[],active_seconds:0,tokens:0,ci_repairs:0,dependencies:[]};
 const markup=taskDialog(t,{}, {tasks:[],project:{repository:'a/b'}});
 assert.ok(!markup.includes('<script>danger'));assert.ok(markup.includes('&lt;img'));
});
test('terminal and paused tasks expose only relevant commands',()=>{
 assert.deepEqual(taskActions({status:'completed'}),[]);
 assert.deepEqual(taskActions({status:'paused'}),['resume','cancel']);
 assert.deepEqual(taskActions({status:'cancelled'}),['resume']);
});
test('overview uses actual counts and empty state; chat defaults to analysis',()=>{
 const state={tasks:[],attentions:[],events:[],project:{repository:'owner/repo',default_branch:'main'},conversations:[],messages:[]};
 assert.ok(overview(state).includes('这里还没有任务'));
 assert.ok(chatPage(state).includes('<option value="analysis">仅分析与建议</option>'));
});
