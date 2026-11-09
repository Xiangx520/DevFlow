import {test} from "node:test";
import assert from "node:assert/strict";
import {parseCommand,records} from "../src/protocol.ts";
test("LF framing preserves Unicode separators",async()=>{
  async function* input(){yield '{"text":"a\u2028b"}\n{"ok":true}\r\n';}
  const rows=[];for await(const row of records(input()))rows.push(JSON.parse(row));
  assert.equal(rows.length,2);assert.equal(rows[0].text,"a\u2028b");
});
test("rejects malformed commands and incomplete records",async()=>{
  assert.throws(()=>parseCommand('{"v":2,"id":"x","type":"status"}'));
  assert.throws(()=>parseCommand('{"v":1,"id":"x","type":"start"}'));
  async function* input(){yield '{"unfinished":';}
  await assert.rejects(async()=>{for await(const _ of records(input())){}});
});

test("UTF-8 split across chunks preserves Chinese",async()=>{
  const bytes=Buffer.from('{"text":"中文"}\n',"utf8");
  async function* input(){for(const byte of bytes)yield Buffer.from([byte]);}
  const rows=[];for await(const row of records(input()))rows.push(JSON.parse(row));
  assert.equal(rows[0].text,"中文");
});
