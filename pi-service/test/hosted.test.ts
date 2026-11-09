import {test} from "node:test";
import assert from "node:assert/strict";
import {attachHostedExecution,HostedExecutionStopped} from "../../vendor/pi/packages/agent/src/hosted.ts";
import type {Agent} from "../../vendor/pi/packages/agent/src/agent.ts";
test("model boundary stops before provider and preserves prior hook",async()=>{
  let calls=0;
  const agent:Pick<Agent,"prepareRequest"|"beforeToolCall">={prepareRequest:()=>{calls++;}};
  const gate=attachHostedExecution(agent,{check:async()=>({stop:true,reason:"budget"}),onBoundary:()=>{}});
  await assert.rejects(async()=>agent.prepareRequest!({} as Parameters<NonNullable<Agent["prepareRequest"]>>[0]),HostedExecutionStopped);
  assert.equal(calls,0);assert.equal(gate.getStopReason(),"budget");
  gate.detach();await agent.prepareRequest!({} as Parameters<NonNullable<Agent["prepareRequest"]>>[0]);
  assert.equal(calls,1);
});
test("tool boundary blocks and terminates without executing previous hook",async()=>{
  let calls=0;
  const agent:Pick<Agent,"prepareRequest"|"beforeToolCall">={beforeToolCall:async()=>{calls++;return undefined;}};
  attachHostedExecution(agent,{check:async()=>({stop:true,reason:"lease_expired"}),onBoundary:()=>{}});
  const result=await agent.beforeToolCall!({toolCall:{name:"bash"}} as Parameters<NonNullable<Agent["beforeToolCall"]>>[0]);
  assert.equal(result?.block,true);assert.equal(result?.terminate,true);assert.equal(calls,0);
});
