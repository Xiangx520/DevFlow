"""Opt-in real Pi + container + local streaming model; no paid or GitHub writes."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import socket
import time
import pytest
import uvicorn
from sqlalchemy import select
from devflow import core
from devflow.api import app_factory
from devflow.config import settings
from devflow.db import transaction
from devflow.execution import DockerExecution
from devflow.models import Project, Task
from devflow.worker import run_execution

pytestmark=pytest.mark.skipif(os.environ.get("DEVFLOW_RUN_DOCKER_TESTS")!="1",reason="explicit real Docker test flag")

def free_port():
    with socket.socket() as s:s.bind(("127.0.0.1",0));return s.getsockname()[1]

class FakeProvider(BaseHTTPRequestHandler):
    calls=0
    mode="write"
    on_first = None
    structured_calls = 0
    pr_writes = 0
    prs = []
    def log_message(self,*args): pass
    def do_GET(self):
        if self.path.startswith("/repos/owner/repo/pulls"):
            return self.respond(FakeProvider.prs)
        if "/actions/runs" in self.path:
            from urllib.parse import urlparse, parse_qs
            sha = parse_qs(urlparse(self.path).query)["head_sha"][0]
            return self.respond({"total_count": 1, "workflow_runs": [{"id": 1, "head_sha": sha,
                                  "status": "completed", "conclusion": "success"}]})
        if self.path.endswith("/status"):
            return self.respond({"total_count": 0})
        return self.respond({}, 404)
    def respond(self, value, status=200):
        body=json.dumps(value).encode()
        self.send_response(status);self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)));self.end_headers();self.wfile.write(body)
    def do_POST(self):
        payload=json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path.startswith("/repos/"):
            FakeProvider.pr_writes += 1
            result={"number": 12, "state": "open", "body": payload["body"]}
            FakeProvider.prs=[result]
            return self.respond(result)
        if not payload.get("stream"):
            FakeProvider.structured_calls += 1
            return self.respond({"choices": [{"message": {"tool_calls": [{"function": {
                "name": "submit", "arguments": json.dumps({"aligned": True})}}]}}], "usage": {"total_tokens": 20}})
        pending=set()
        for message in payload["messages"]:
            role=message.get("role")
            if role=="tool":
                pending.discard(message.get("tool_call_id"))
            else:
                if pending:
                    self.send_response(400);self.end_headers()
                    self.wfile.write(b'{"error":{"message":"missing tool result after interrupted session"}}')
                    return
                pending.update(call["id"] for call in message.get("tool_calls",[]))
        if pending:
            self.send_response(400);self.end_headers()
            self.wfile.write(b'{"error":{"message":"unanswered tool call"}}');return
        FakeProvider.calls+=1
        if FakeProvider.calls == 1 and FakeProvider.on_first:
            FakeProvider.on_first()
        name="write" if FakeProvider.calls<=2 else "submit_for_review"
        args={"path":"value.py","content":"VALUE = 99\n" if FakeProvider.calls==1 else "VALUE = 2\n"} if name=="write" else {"summary":"Fixed value and prepared for host verification"}
        if FakeProvider.mode=="sleep":
            name="bash";args={"command":"printf 'PROGRESS = 1\\n' > recovery.py; touch /workspace/sleep-started; sleep 60"}
        self.send_response(200);self.send_header("Content-Type","text/event-stream");self.end_headers()
        for delta,finish in [
            ({"role":"assistant","tool_calls":[{"index":0,"id":"call-"+str(FakeProvider.calls),"type":"function",
              "function":{"name":name,"arguments":json.dumps(args)}}]},None),({}, "tool_calls")]:
            item={"id":"test","object":"chat.completion.chunk","created":1,"model":"test",
                  "choices":[{"index":0,"delta":delta,"finish_reason":finish}]}
            if finish:item["usage"]={"prompt_tokens":10,"completion_tokens":10,"total_tokens":20}
            self.wfile.write(("data: "+json.dumps(item)+"\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n");self.wfile.flush()

async def test_real_pi_edits_code_and_host_independently_verifies(project,monkeypatch,request):
    import docker
    client=docker.from_env()
    try:client.images.get("devflow-runner:local")
    except docker.errors.ImageNotFound:pytest.fail("build devflow-runner:local first")
    network="devflow-test-"+__import__("uuid").uuid4().hex
    client.networks.create(network)
    provider=ThreadingHTTPServer(("0.0.0.0",0),FakeProvider);FakeProvider.calls=0;FakeProvider.mode="write"
    FakeProvider.prs=[]; FakeProvider.pr_writes=0; FakeProvider.structured_calls=0
    provider_thread=threading.Thread(target=provider.serve_forever,daemon=True);provider_thread.start()
    api_port=free_port()
    monkeypatch.setenv("DEVFLOW_RUNNER_NETWORK",network)
    monkeypatch.setenv("DEVFLOW_INTERNAL_URL",f"http://host.docker.internal:{api_port}")
    monkeypatch.setenv("DEVFLOW_LLM_BASE_URL",f"http://127.0.0.1:{provider.server_address[1]}/v1")
    monkeypatch.setenv("DEVFLOW_LLM_API_KEY","local-simulation")
    monkeypatch.setenv("DEVFLOW_LLM_MODEL","test")
    monkeypatch.setenv("DEVFLOW_GITHUB_API_URL", f"http://127.0.0.1:{provider.server_address[1]}")
    settings.cache_clear()
    server=uvicorn.Server(uvicorn.Config(app_factory(),host="0.0.0.0",port=api_port,log_level="error",lifespan="off"))
    thread=threading.Thread(target=server.run,daemon=True);thread.start()
    for _ in range(100):
        if server.started:break
        await asyncio.sleep(.1)
    class ExistingCheckout(DockerExecution):
        def prepare(self,task,project): pass
        def repository_url(self, project): return "/workspace/remote.git"
    driver=ExistingCheckout()
    with transaction() as db:
        p=core.get(db,Project,project)
        p.config={"verify_commands":["python3 -c \"from value import VALUE; assert VALUE == 2\""]}
        task=core.create_task(db,project,"Fix value","Change VALUE from 1 to 2",["VALUE equals 2"])
        task_id=task.id
        attempt,capability=core.claim_task(db,task.id,"docker-test");attempt_id=attempt.id
    def insert_constraints():
        from devflow import specs, steering
        with transaction() as db:
            current=core.get(db,Task,task_id)
            specs.update(db, core.get(db,Project,project), "VALUE must equal 2; do not write 99.")
            steering.steer(db, current, "Wrong direction: preserve the mandate and write VALUE = 2, never 99.",
                           kind="correction", source="watchdog")
    FakeProvider.on_first=insert_constraints
    try:
        driver.client.volumes.create(driver.volume(task_id))
        script="from pathlib import Path; import subprocess; Path('value.py').write_text('VALUE = 1\\n'); Path('.gitignore').write_text('__pycache__/\\n'); subprocess.run(['git','init'],check=True); subprocess.run(['git','checkout','-b','devflow/"+task_id+"'],check=True); subprocess.run(['git','add','.'],check=True); subprocess.run(['git','-c','user.name=Test','-c','user.email=test@localhost','commit','-m','initial'],check=True)"
        script += "; subprocess.run(['git','init','--bare','/workspace/remote.git'],check=True)"
        await asyncio.to_thread(driver.helper,task_id,["python3","-c",script])
        await asyncio.wait_for(run_execution(attempt_id,capability,driver),timeout=120)
        with transaction() as db:
            task=core.get(db,Task,task_id);project_row=core.get(db,Project,project)
            assert task.status=="waiting_external",task.wait_reason
            assert task.wait_reason=="host verification"
        result=await asyncio.to_thread(driver.verify,task,project_row)
        assert result["passed"] and len(result["sha"])==40
        assert FakeProvider.calls==3
        # Full host verification -> independent HTTP review -> real Git push -> simulated PR/CI.
        from devflow import effects, watcher, specs
        from devflow.models import Effect
        from devflow.adapters import GitHub
        with transaction() as db:
            verify=db.scalar(select(Effect).where(Effect.task_id==task_id,Effect.kind=="verify"))
            verify.owner="host";verify_id=verify.id
        await effects.execute(verify_id,"host",GitHub(),driver)
        await watcher.review_task(task_id,force=True)
        with transaction() as db:
            current=core.get(db,Task,task_id)
            assert specs.complete(db,current)
            publish=db.scalar(select(Effect).where(Effect.task_id==task_id,Effect.kind=="publish"))
            assert publish is not None
            publish.owner="publish"; publish_id=publish.id
        await effects.execute(publish_id,"publish",GitHub(),driver)
        await effects.execute(publish_id,"publish",GitHub(),driver)
        assert FakeProvider.pr_writes==1
        with transaction() as db:
            ci=db.scalar(select(Effect).where(Effect.task_id==task_id,Effect.kind=="check_ci"))
            ci.owner="ci";ci_id=ci.id
        await effects.execute(ci_id,"ci",GitHub(),driver)
        with transaction() as db: assert core.get(db,Task,task_id).status=="ready_for_review"
        remote=await asyncio.to_thread(driver.helper,task_id,["git","--git-dir=/workspace/remote.git","rev-parse","refs/heads/devflow/"+task_id])
        assert len(remote.strip())==40
        await asyncio.to_thread(driver.helper,task_id,["python3","-c","from pathlib import Path; Path('large.txt').write_text('x'*30000)"])
        large=await asyncio.to_thread(driver.verify,task,project_row)
        assert large["passed"] and large["diff_truncated"] and len(large["diff"])==24000
        with transaction() as db:
            current=core.get(db,Task,task_id)
            assert current.tokens==80, "proxy must settle coding and supervision model usage"
            previous_session=current.session_id
            core.command(db,task_id,"pause")
            core.command(db,task_id,"resume")
            next_attempt,next_capability=core.claim_task(db,task_id,"recovered")
            next_id=next_attempt.id
            assert current.session_id==previous_session
        FakeProvider.mode="sleep"
        resumed=asyncio.create_task(run_execution(next_id,next_capability,driver))
        started=False
        for _ in range(200):
            containers=client.containers.list(filters={"label":"devflow.task="+task_id})
            if containers:
                result=await asyncio.to_thread(containers[0].exec_run,["test","-f","/workspace/sleep-started"])
                if result.exit_code==0:
                    started=True;break
            if resumed.done():break
            await asyncio.sleep(.1)
        assert started,"restored Pi session did not start the test command"
        # Interrupt the worker with a tool in flight, expire its lease, then start a replacement process.
        resumed.cancel()
        with pytest.raises(asyncio.CancelledError):await resumed
        from devflow import watcher
        with transaction() as db:
            interrupted=core.get(db,Task,task_id)
            interrupted.lease_until=time.time()-1
            watcher.run_liveness(db)
            assert interrupted.status=="queued" and interrupted.session_id==previous_session
            recovered,recovered_capability=core.claim_task(db,task_id,"new-worker")
            recovered_id=recovered.id
        await asyncio.to_thread(driver.helper,task_id,["python3","-c",
            "from pathlib import Path; assert Path('recovery.py').read_text()=='PROGRESS = 1\\n'; Path('/workspace/sleep-started').unlink(missing_ok=True)"])
        resumed=asyncio.create_task(run_execution(recovered_id,recovered_capability,driver))
        started=False
        for _ in range(200):
            containers=client.containers.list(filters={"label":"devflow.task="+task_id})
            if containers:
                result=await asyncio.to_thread(containers[0].exec_run,["test","-f","/workspace/sleep-started"])
                if result.exit_code==0:
                    started=True;break
            if resumed.done():break
            await asyncio.sleep(.1)
        assert started,"interrupted Pi transcript could not be resumed with strict tool-result validation"
        next_id,next_capability=recovered_id,recovered_capability
        with transaction() as db:core.command(db,task_id,"cancel")
        await asyncio.wait_for(resumed,timeout=10)
        assert not client.containers.list(filters={"label":"devflow.task="+task_id})
        with transaction() as db:
            current=core.get(db,Task,task_id)
            assert current.status=="cancelled" and current.session_id==previous_session
            with pytest.raises(core.Conflict):core.active_attempt(db,next_id,next_capability)
    finally:
        for container in client.containers.list(all=True,filters={"label":"devflow.task="+task_id}):
            container.remove(force=True)
        client.volumes.get(driver.volume(task_id)).remove()
        client.networks.get(network).remove()
        server.should_exit=True;thread.join(timeout=10)
        provider.shutdown();provider.server_close()
        request.node.simulation_metrics={"model_calls": {"coding": FakeProvider.calls, "watchdog": FakeProvider.structured_calls},
                                         "external_writes": {"pr": FakeProvider.pr_writes, "git_push": 1},
                                         "real_pi": True, "persistent_recovery": True}
